"""
CP4.7-E2E-BACKEND 端到端集成测试（content-service → ai-service → audio_url）。

测试范围：
1. 完整链路：公众号 URL → 抓取 → 蒸馏 → audio_url
2. 配额不足 → 402/403
3. 不支持的 URL → 边界处理

所有请求都走网关（gateway）—— 网关对这三个端点都有**显式路由**：
    POST /api/v1/articles/{id}/distill  → ai-service
    GET  /api/v1/distill/{task_id}       → ai-service
    GET  /api/v1/articles/{id}/status    → content-service
（见 api-gateway/config.py 的 CP4.7.1 与 api-gateway/main.py 的 `_resolve_target`；
查询参数整条透传，见 `params=request.query_params`。）

⚠️ 早期版本这里**直连 ai-service**（端口从网关 +3 推导），而那台机的 8103 是
**生产**端口 ⇒ 会把测试任务派进生产队列。已改回全部走网关 ——
理由与事故记录见下面「不要在这个文件里直连 ai-service」那段注释。
"""

import asyncio
import os
import uuid
from urllib.parse import urlparse

import httpx
import pytest

# 集成测试需要 gateway/user/content/ai 4 个服务真跑，CI workflow 只起 PG/Redis，
# 服务不在。CI 上跳过（CP6.12），本地手跑保留测试。
skip_in_ci = pytest.mark.skipif(
    os.getenv("CI") is not None,
    reason="integration test requires running services (skipped in CI, run locally with services up)",
)

# 网关地址。默认指向隔离实例（18100），**不再默认打生产 8100** ——
# 这个文件里的用例会真的建用户、建文章、派蒸馏任务，2026-10-02 实测因为
# 默认值是 8100，一次全量 pytest 就往生产库灌了数据并占了 TTS 队列。
# 隔离后端的起法见 admin-web/e2e-backend.sh。
GATEWAY_URL = os.getenv("STASHBOX_GATEWAY", "http://127.0.0.1:18100")

# ── 剪藏夹具 URL ──────────────────────────────────────────────────────────
#
# ⚠️ 2026-10-25 换掉 example.org，**因为它暴露了一个真问题**：
#
# 原来的夹具是 `https://example.org/`（IANA 保留示例域名）。它一直能用，
# 但今天全量 pytest 出现 1 failed、单跑该文件出现 2 failed（**且失败的是
# 不同的用例**），全都卡在「建文章」这一步，错误是
#     502 {"code":2002,"message":"没能提取出文章正文…"}
#
# 顺着 content-service 的日志查到了根因（不是抖动，也不是代码回归）：
#     fetch_parse_escalate url=https://example.org/ tier=http
#         code=fetcher.parse msg=no article content extracted
#     fetch_ok url=https://example.org/ tier=browser tried=['http', 'browser']
# ⇒ **example.org 的正文太短，http 通道提取不出来**，靠**升级到 browser 通道**才成功。
#
# 而**生产上 browser 通道是关的**：`STASHBOX_FETCH_BROWSER_TIER=0`
# （chromium RSS 300MB+，而 content-service 的 MemoryMax 只有 224MB ——
# 开浏览器通道就等于给自己埋一个 OOM；README §9.4 有记录）。
# ⇒ 本地 e2e（有 browser）时好时坏，**生产上则必然剪不动这个页面**。
#
# ⚠️ 这暴露的通用教训：**测试夹具必须在「生产的实际开关」下也能过**。
# 用一个生产上被关掉的降级通道当夹具 ⇒ 测试间歇红、生产却是好的，
# 于是人会一直「重跑」而永远找不到根因。
#
# 现在这个夹具的判据（2026-10-25 实测，逐个剪一遍看日志里的 tier）：
#     https://www.iana.org/domains/example      tier=http  ✅ 生产能剪
#     https://httpbin.org/html                   tier=http  ✅
#     https://www.rfc-editor.org/rfc/rfc9110.html tier=http  ✅
#     https://www.python.org/about/              tier=http  ✅
#     https://blog.python.org/                   tier=http  ✅
#     https://example.org/  /  example.com/      tier=browser ❌ 生产剪不动
# ⇒ 选 **iana.org 的说明页**：语义上仍是「保留示例域名」，
# 且它的正文长度过了提取器的下限，**http 通道直接就能提取**。
FIXTURE_URL = "https://www.iana.org/domains/example"

if urlparse(GATEWAY_URL).port in {8100, 8101, 8102, 8103, 8104}:
    raise RuntimeError(
        f"拒绝执行：STASHBOX_GATEWAY={GATEWAY_URL} 指向生产端口，"
        "这套 integration 用例会污染生产库并占住 TTS 队列。"
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _login_via_wechat(code: str = "cp47_e2e_test") -> tuple[str, str]:
    """登录，返回 (access_token, user_id)。"""
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        resp = await client.post("/api/v1/auth/wechat-login", json={"code": code})
        assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
        data = resp.json()
        return data["access_token"], data["user_id"]


# ── 等待预算 ────────────────────────────────────────────────────────────────
#
# ⚠️ 2026-10-25 定位到的真根因（这个文件之前「时红时绿」的原因）：
#
# 这个 e2e 环境的 arq 队列是**全栈共享**的，而**每一次 POST /api/v1/articles
# 都会自动派发一次真实的蒸馏任务**（真实 LLM + 真实 TTS，实测单篇 100~160s）。
# 上一轮跑测试留下的任务会排在下一轮前面 ——
# arq worker 日志里那一行 `delayed=` 就是**排队时长**
# （arq 源码 worker.py:593：`delayed=(start_ms - score)/1000`），实测那一轮：
#
#     15:50:30 → delayed=1158.04s      （排了 19 分钟）
#     16:03:19 → delayed=1634.00s      （排了 27 分钟）
#     16:08:55 → delayed=1849.33s      （排了 31 分钟）
#
# 而这里原来的等待预算是**拍脑袋的单一常数**（用例1 等 600s、用例2 等 900s）⇒
# 真实耗时（排队 + 执行）一旦超过预算，用例就红，**而链路本身没坏**
# （同一个任务后来正常跑完了，worker 日志里有对应的 `←` 行）。
# 2026-10-25 实测那一轮：
#   · 用例1 的任务排了 1123s 才起跑（预算 600s）⇒ 必红；
#   · 用例2 的任务排了 780s + 执行 138s = 918s，**差 18 秒**超过 900s ⇒ 也红。
# 而把队列排空后用**同一份代码**重跑 ⇒ 4 passed（366s）。
#
# ⇒ 修法不是「把常数调大一点」，而是**把排队和执行分开算**：
#   · **排队**是环境属性（队列里排在我前面有几个任务），不是这条链路的行为；
#   · **执行**才是这条链路的行为（起跑后多久出结果）。
# 怎么知道「起跑了」？content-service 的 `GET /articles/{id}/status` 里的 `stage`：
# ai-service 的 agent 第一个节点（fetch）一开跑就写 stage
# （common/distill_stage.py），跑完还会清掉 ⇒ stage 出现 = worker 已经取走任务。
#
# 预算用**轮询次数**表达（每次轮询间隔 1 秒 ⇒ 次数 ≈ 秒），和原实现的
# `for i in range(timeout)` 同一个口径。

#: 排队阶段的预算（轮询次数）。
#: 依据：实测这个共享队列的排队最长 1849s ⇒ 取 2400（40 分钟）留足余量。
#: 这不是无限等：超过它说明 worker 根本没在消费队列，要报错而不是挂着。
QUEUE_BUDGET_POLLS = 2400

#: 执行阶段的预算（轮询次数）：起跑之后多久必须出结果。
#: 依据：arq worker 实测单任务执行 103~160s（`←` 行），ai-service/main.py 的
#: 注释也记着「实测单篇 2~4 分钟」⇒ 600 有 3~6 倍余量。
EXEC_BUDGET_POLLS = 600


async def _read_stage(client: httpx.AsyncClient, article_id: str, auth: dict) -> str | None:
    """读文章当前的蒸馏阶段；None = worker 还没取走任务（或已跑完并清掉了）。

    为什么要它：把「还在排队」和「已经开跑」分开 —— 前者是队列积压（环境属性，
    不该让用例背锅），后者才是这条链路自己的责任。
    """
    resp = await client.get(f"/api/v1/articles/{article_id}/status", headers=auth)
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get("stage")
    except Exception:  # noqa: BLE001 —— 读不到阶段不能影响主流程
        return None


async def _poll_task_status(
    client: httpx.AsyncClient,
    task_id: str,
    auth: dict,
    article_id: str,
    *,
    queue_budget_polls: int = QUEUE_BUDGET_POLLS,
    exec_budget_polls: int = EXEC_BUDGET_POLLS,
    poll_interval_s: float = 1.0,
) -> dict:
    """轮询 GET /api/v1/distill/{task_id} 直到对外状态是 'ready'。

    CP9.x 起蒸馏是真实 LLM（火山 ark）+ 真实 TTS（indextts）流水线，单任务
    端到端需要数分钟（step1 结构化 → step2 改写 → step3 TTS → step4 拼接），
    旧 60s 是 mock LLM 时代的设置，会误报超时。
    POST /distill/start 只创建 DistilledArticle(status=queued)，不更新 Article.status。
    因此轮询 Article.status 永远得到 'pending'。正确做法是轮询 task status。

    ⚠️ 判据必须是**对外 5 态**，不是内部状态机（2026-10-25 修）。
    `GET /api/v1/distill/{id}` 走 `common/distill_status.py::derive_distill_status()`：
        产物表 done   → 对外 **ready**
        产物表 failed + 还在重试 → 对外 distilling
        产物表 queued / step1~4 → articles.status（pending 或 distilling）
    原来这里等的是内部值 "done" ⇒ **永远等不到**（外层看到的是 "ready"）⇒
    用例一路轮询到超时才失败，症状像「蒸馏卡住」，实际是判据用错了状态层。

    ⚠️ 预算分**排队段 / 执行段**两段（依据见文件头「等待预算」的说明）：
    单一常数在共享队列有积压时会误报「超时」，而任务其实只是排在别人后面。
    排队段靠 `stage` 判定是否已起跑（见 `_read_stage`）。
    """
    data: dict = {}
    stage: str | None = None
    running = False
    phase_polls = 0
    total_polls = 0
    while True:
        # 先读 stage、再读任务状态：这样「worker 取走了任务」这一事实
        # 在同一轮里就能被看到，不会晚一轮才切到执行段。
        if not running:
            stage = await _read_stage(client, article_id, auth)
            if stage:
                running = True
                phase_polls = 0  # 执行段重新计时

        resp = await client.get(f"/api/v1/distill/{task_id}", headers=auth)
        assert resp.status_code == 200, f"task status poll failed: {resp.status_code} {resp.text}"
        data = resp.json()
        if data.get("status") == "ready":
            return data
        if data.get("status") in ("failed", "error"):
            pytest.fail(f"蒸馏任务失败: {data}")

        budget = exec_budget_polls if running else queue_budget_polls
        phase_polls += 1
        total_polls += 1
        if phase_polls > budget:
            phase = "已经起跑但没跑完" if running else "还在排队（worker 还没取走任务）"
            pytest.fail(
                f"任务 {task_id} 等待超时：{phase}；"
                f"本段已等 {phase_polls}s（预算 {budget}s），累计 {total_polls}s；"
                f"最后 status={data.get('status')} stage={stage}"
            )
        await asyncio.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# 测试 1：完整链路（mock LLM）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@skip_in_ci
async def test_wechat_url_full_distill_pipeline():
    """
    完整链路：POST /articles → content-service 抓取 →
    POST /distill/start → ai-service 蒸馏（mock LLM）→ audio_url 落库 →
    GET /articles/{id}/audio-url 返回合法 URL。

    验证点：
    - 文章创建成功
    - 蒸馏任务入队（202）
    - 状态轮询在 30s 内完成
    - audio_url 是合法 http/https URL
    """
    token, _ = await _login_via_wechat(f"cp47_full_{uuid.uuid4().hex[:8]}")
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=60.0) as client:
        # Step 1: 创建文章
        #
        # ⚠️ 夹具 URL 见文件头的 FIXTURE_URL —— 必须是 **http 通道就能提取正文**的页面
        # （生产上 browser 通道是关的，见 FIXTURE_URL 上面的说明）。
        url = FIXTURE_URL
        create_resp = await client.post(
            "/api/v1/articles",
            json={"url": url, "source": "web"},
            headers=auth,
        )
        assert create_resp.status_code in (
            200,
            201,
        ), f"创建文章失败: {create_resp.status_code} {create_resp.text}"
        create_data = create_resp.json()
        # POST /api/v1/articles 返回 {article_id, url, status, quota_used, ...}
        article_id = create_data.get("article_id") or create_data.get("id")
        assert article_id, f"响应无 article_id: {create_data}"
        print(f"[CP4.7] 文章创建成功: id={article_id}")

        # Step 2: 启动蒸馏（走 /api/v1/distill/start → ai-service）
        # 先 GET 文章获取 title 和 url
        art_resp = await client.get(f"/api/v1/articles/{article_id}", headers=auth)
        assert art_resp.status_code == 200, f"获取文章失败: {art_resp.status_code}"
        art_data = art_resp.json()

        distill_resp = await client.post(
            "/api/v1/distill/start",
            json={
                "article_id": article_id,
                "url": art_data["url"],
                "title": art_data.get("title") or "测试文章",
            },
            headers=auth,
        )
        assert (
            distill_resp.status_code == 200
        ), f"启动蒸馏失败: {distill_resp.status_code} {distill_resp.text}"
        distill_data = distill_resp.json()
        task_id = distill_data.get("task_id")
        job_id = distill_data.get("job_id") or task_id
        print(f"[CP4.7] 蒸馏任务已入队: task_id={task_id}, job_id={job_id}")

        # Step 3: 轮询任务状态直到 ready（真实 LLM+TTS 流水线）
        # ⚠️ 对外状态是 "ready"（内部 done 经 common/distill_status.derive_distill_status 派生），
        # 不是内部状态机的 "done" —— 判据用错状态层会一路轮询到超时。
        # ⚠️ 预算是**两段**的（排队段 / 执行段），依据见文件头「等待预算」。
        task_data = await _poll_task_status(client, task_id, auth, article_id)
        assert task_data["status"] == "ready", f"蒸馏未完成: status={task_data['status']}"
        print(f"[CP4.7] 蒸馏完成: task_status={task_data['status']}")

        # Step 4: 获取 audio_url
        audio_resp = await client.get(
            f"/api/v1/articles/{article_id}/audio-url",
            headers=auth,
        )
        assert (
            audio_resp.status_code == 200
        ), f"获取 audio_url 失败: {audio_resp.status_code} {audio_resp.text}"
        audio_data = audio_resp.json()
        audio_url = audio_data.get("audio_url")
        assert audio_url, f"audio_url 为空: {audio_data}"
        assert audio_url.startswith(("http://", "https://")), f"audio_url 格式非法: {audio_url}"
        assert any(
            ext in audio_url for ext in (".mp3", ".m4a", ".aac", ".wav")
        ), f"audio_url 非音频格式: {audio_url}"
        print(f"[CP4.7] audio_url 合法: {audio_url}")
        print("[CP4.7] 完整链路验证通过")


# ---------------------------------------------------------------------------
# 测试 1b：蒸馏失败退还配额（CP1.7 + CP3）
# ---------------------------------------------------------------------------


# ⚠️ **不要在这个文件里直连 ai-service**（2026-10-25 改）。
#
# 原来这里有个 `_ai_service_url()`：从网关端口 +3 推出 ai-service 的直连地址，
# 理由是「`/articles/{id}/distill` 在 gateway 里 fallback 到 content-service，
# 那边没这个端点」。但那个前提早就**不成立**了 ——
# 网关现在对这条路径是**显式路由**（api-gateway/config.py 的 CP4.7.1，
# 以及 api-gateway/main.py 的 `_resolve_target`：`articles/` 开头的路径里
# 带 `/distill` 的优先走 ai-service），查询参数也整条透传。
#
# 而直连的代价是实打实的：这台机的 **8103 是生产 ai-service 的端口**，
# 且它真的在监听（本机 lsof 实测）。当时写死 `http://localhost:8103`，
# 于是把 STASHBOX_GATEWAY 指向 e2e 网关（:18100）跑这套用例时，
# 会发生最坏的一种组合：
#
#     用户/文章建在**隔离库**（stashbox_e2e）
#     蒸馏任务却入队到**生产** ai-service
#
# —— 用例在「隔离环境」跑，却往生产队列派活、占 TTS、写生产库。
# 和 e2e-backend.sh 注释里记的那次事故同一类（那次是网关上游没全指到
# e2e 实例，音色写进了生产 bucket）：**静默默认值比没有默认值危险**。
#
# ⇒ 结论不是「把端口推导做得更聪明」，而是**根本不需要直连**：
# 网关已经把三个相关端点都显式路由好了，统一走网关即可
# （`/articles/{id}/distill`→ai-service、`/distill/{id}`→ai-service、
# `/articles/{id}/status`→content-service），
# 而且那才是 App 实际走的那条路。


@pytest.mark.asyncio
@skip_in_ci
async def test_distill_failure_refund_quota():
    """
    蒸馏失败（simulate_failure=True）→ 配额退还。

    ⚠️ 这里有个**结构性的坑**（2026-10-25 才定位到，之前这条用例一直红）：

    剪藏**本身就已经自动派了一次蒸馏**（content-service 建库后调 trigger_distill），
    所以「创建文章」这一步就有一个任务在跑。而
    `POST /api/v1/articles/{id}/distill` 的**入队幂等守卫**规定：
    已有 queued/running 的任务 → **不重复入队**
    （防的是同一篇文章入队两个 job ⇒ TTS 跑两遍、白烧一倍算力）。

    ⇒ 原用例在剪藏后立刻 POST `simulate_failure=true`，会命中那个守卫：
    返回 **200 + job_id="" + 现有任务**，而 `simulate_failure` 这个标志
    **根本没生效** —— worker 照常跑完整条流水线（真实 LLM + 真实 TTS），
    任务最后是 done 而不是 failed。

    症状极像「失败路径坏了」，实际是「失败压根没被触发」。
    判据是 `job_id`：守卫命中时它是空串。

    修法：先等**自动派的那次蒸馏**跑完（status=done，守卫不再命中），
    再 POST `simulate_failure=true` → 这时才会真的入队一个「注定失败」的任务
    （失败在 agent 之前抛出，不烧 LLM/TTS，几秒内就结束）。

    另注：`/articles/{id}/distill` 在 gateway 里**显式路由**到 ai-service
    （不是 fallback —— 见 api-gateway/config.py 的 CP4.7.1 与 main.py 的
    `_resolve_target`），查询参数也整条透传（`params=request.query_params`）⇒
    这里统一走网关即可，不必直连 ai-service（网关才是 App 实际走的那条路）。
    """
    import uuid

    token, _ = await _login_via_wechat(f"cp47_fail_{uuid.uuid4().hex[:8]}")
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=60.0) as client:
        # 夹具 URL 见文件头 FIXTURE_URL（http 通道就能提取，生产也剪得动）。
        # ⚠️ 外部站点仍可能**偶发**抓取失败（网络抖动/限流）⇒ 502 时要能一眼看出
        #    「是外部站点没抓到」而不是「契约不对」。
        create_resp = await client.post(
            "/api/v1/articles",
            json={"url": FIXTURE_URL, "source": "web"},
            headers=auth,
        )
        assert create_resp.status_code in (200, 201), (
            f"创建文章失败 {create_resp.status_code}: {create_resp.text}\n"
            "  若 code=2002/2001 → **外部站点这次没抓到**（网络抖动/限流），可重跑；\n"
            "  但若 code=2002 且日志里 tier=browser ⇒ 夹具又选成需要 browser 通道的页面了\n"
            "  （生产 STASHBOX_FETCH_BROWSER_TIER=0，剪不动）——换夹具，别重跑。"
        )
        create_data = create_resp.json()
        article_id = create_data.get("article_id") or create_data.get("id")
        auto_task_id = create_data.get("task_id")
        print(f"[CP4.7] 文章已创建: {article_id}（自动蒸馏任务 {auto_task_id}）")

        # 剪藏后配额应是 1
        quota_before = (await client.get("/api/v1/users/me/quota", headers=auth)).json()
        assert quota_before["quota_used"] == 1, quota_before

        # 等自动派的那次蒸馏跑完 —— 否则幂等守卫会吞掉 simulate_failure
        assert auto_task_id, "创建响应里没有 task_id，无法等自动蒸馏"
        await _poll_task_status(client, auto_task_id, auth, article_id)

        # 现在再请求「模拟失败」：守卫不该命中（已有任务是 done）
        distill_resp = await client.post(
            f"/api/v1/articles/{article_id}/distill",
            headers=auth,
            params={"simulate_failure": "true"},
        )
        assert (
            distill_resp.status_code == 200
        ), f"distill 触发失败: {distill_resp.status_code} {distill_resp.text}"
        body = distill_resp.json()
        task_id = body.get("task_id")
        # ⚠️ 关键断言：job_id 非空 = **真的入队了一个新任务**。
        # 为空就说明又命中了幂等守卫 → simulate_failure 没生效 → 后面必然是 done。
        assert body.get("job_id"), f"没有入队新任务（守卫命中，simulate_failure 会失效）: {body}"
        print(f"[CP4.7] 模拟失败任务已入队: task_id={task_id} job_id={body['job_id']}")

        # 等 worker 处理完（最终失败）
        #
        # ⚠️ 判据要打在**用户真正看到的那条链路**上：
        # `GET /api/v1/distill/{task_id}`（ai-service）读的是 distilled_articles.status，
        # 而失败路径按设计**只写 articles.status='failed'**、不动产物表
        # （中间态更不能写 'queued'/'running'，否则幂等守卫会挡住用户重试），
        # ⇒ 那个端点对一次终局失败永远显示 "pending"。
        # 用户看到的状态走 content-service 的 `GET /api/v1/articles/{id}/status`
        # （`_derive_status` 以 articles.status 为准）—— 那里才是 "failed"。
        #
        # Arq 的重试退避是 15s + 30s ⇒ 终局失败在 ~1 分钟后出现。
        for _ in range(180):
            st = await client.get(f"/api/v1/articles/{article_id}/status", headers=auth)
            assert st.status_code == 200, f"文章状态查询失败: {st.status_code} {st.text}"
            status_data = st.json()
            if status_data.get("status") == "failed":
                break
            await asyncio.sleep(1)
        else:
            pytest.fail(f"文章状态没有变成 failed: {status_data}")

        # 失败 ⇒ 配额退还（剪藏时扣的那 1 次要退回来）
        #
        # ⚠️ 退款发生在**文章落 failed 之后**（distill_task 里是先 commit
        # articles.status='failed'，再 _refund_quota_once）⇒ 看到 failed 的
        # 那一刻配额**可能还没退**。原来这里只读一次 ⇒ 竞态（差几十毫秒就红）。
        # 改成有界轮询：给退款留 30 秒窗口，而不是赌它已经发生。
        quota_after: dict = {}
        for _ in range(30):
            quota_after = (await client.get("/api/v1/users/me/quota", headers=auth)).json()
            if quota_after["quota_used"] == 0:
                break
            await asyncio.sleep(1)
        assert quota_after["quota_used"] == 0, f"蒸馏失败后配额应退还: {quota_after}"
        print(f"[CP4.7] 蒸馏失败验证通过，配额已退还: {quota_after}")


# ---------------------------------------------------------------------------
# 测试 2：unsupported URL 边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@skip_in_ci
async def test_unsupported_url_returns_envelope_without_creating_article():
    """
    抓不到正文 → **当场报错，不建文章也不扣配额**（2026-10-03 起的契约）。

    ⚠️ 这条断言**被推翻过**：原用例断言「URL 不支持 → 200 + 文章入库(pending)」
    （CP2.7 的结论）。2026-10-03 改成「先抓后扣」：抓取失败当场返回 2001/2002，
    既不建库也不计费 —— 因为建一条无正文的文章会派一个**注定失败**的蒸馏任务
    （白烧 LLM token），而用户要等 12 分钟后才看到失败。

    所以这里断言的是**业务信封**（安卓的 `isCaptureFetchFailed(2001|2002)`
    靠它区分「抓不到」与「服务坏了」），而不是「返回 200 就行」。

    实测（2026-10-25，隔离栈 18100）：
      https://lobste.rs/        → 502 {"code":2002,"message":"没能提取出文章正文…"}
      https://example.com/article-1 → 502 {"code":2002,"message":"这篇文章已被删除或链接已失效"}
      （example.com 的**子路径**是硬 404；而 example.org 的**根路径**虽然能抓，
        但靠的是 browser 降级通道 ⇒ 生产上剪不动，见文件头 FIXTURE_URL 的说明）
    """
    token, _ = await _login_via_wechat(f"cp47_unsupported_{uuid.uuid4().hex[:8]}")
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        # 用一个已知的"抓不到正文"URL：lobste.rs 首页（正文长度不达下限）
        url = "https://lobste.rs/"
        create_resp = await client.post(
            "/api/v1/articles",
            json={"url": url, "source": "web"},
            headers=auth,
        )

        # 抓不到正文 → 当场报错（不是 200，也不是裸 500）
        assert create_resp.status_code == 502, create_resp.text
        body = create_resp.json()
        # 业务信封：安卓靠 bizCode 走「链接失效」分支；裸 500 会让它误报「服务暂不可用」
        assert body.get("code") in (2001, 2002), create_resp.text
        assert body.get("message"), create_resp.text
        print(f"[CP4.7] 抓不到正文 → 业务信封: code={body.get('code')}")


# ---------------------------------------------------------------------------
# 测试 3：article status + audio-url 边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@skip_in_ci
async def test_get_audio_before_distill_returns_404():
    """
    蒸馏前 GET /articles/{id}/audio-url → 404（audio not ready）。

    验证点：未蒸馏的文章 audio-url 返回 404。
    """
    token, _ = await _login_via_wechat(f"cp47_audio_{uuid.uuid4().hex[:8]}")
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        # 创建文章（不蒸馏）
        create_resp = await client.post(
            "/api/v1/articles",
            json={"url": FIXTURE_URL, "source": "web"},
            headers=auth,
        )
        assert create_resp.status_code in (200, 201), create_resp.text
        article_id = create_resp.json().get("article_id") or create_resp.json().get("id")

        # 获取 audio-url（未蒸馏 → 404）
        audio_resp = await client.get(
            f"/api/v1/articles/{article_id}/audio-url",
            headers=auth,
        )
        assert (
            audio_resp.status_code == 404
        ), f"未蒸馏文章应返回 404，实际 {audio_resp.status_code}: {audio_resp.text}"
        print("[CP4.7] 未蒸馏 audio-url 正确返回 404")


# ---------------------------------------------------------------------------
# 等待预算的回归测试（纯本地，不碰任何服务 ⇒ CI 也跑）
# ---------------------------------------------------------------------------


def _fake_gateway_after(worker_picks_up_at: int, completes: bool = True):
    """造一个假的网关：worker 在第 N 次轮询之后才把任务取走。

    返回 (handler, state)：
    · `GET /api/v1/distill/{id}` —— 取走之前一直是 "distilling"，之后 "ready"
    · `GET /api/v1/articles/{id}/status` —— 取走之前 stage=None，之后 stage="tts"

    `completes=False` 用来演「worker 取走了任务但一直没跑完」。

    stage 就是「worker 已经取走任务」的信号（ai-service 的 agent 第一个节点
    一开跑就写它，见 common/distill_stage.py）。
    """
    state = {"polls": 0, "taken": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/distill/"):
            state["polls"] += 1
            if state["taken"] and completes:
                return httpx.Response(200, json={"status": "ready", "task_id": "dst_fake"})
            return httpx.Response(200, json={"status": "distilling", "task_id": "dst_fake"})
        if path.endswith("/status"):
            if state["polls"] >= worker_picks_up_at:
                state["taken"] = True
                return httpx.Response(200, json={"status": "distilling", "stage": "tts"})
            return httpx.Response(200, json={"status": "distilling", "stage": None})
        return httpx.Response(404, json={"code": 40400})

    return handler, state


@pytest.mark.asyncio
async def test_poll_survives_queue_delay_beyond_legacy_budget():
    """排队超过**老预算**才起跑 ⇒ 老口径必红、两段预算必须绿。

    这是 2026-10-25 那个「时红时绿」的最小复现：
    共享队列里有别的任务排在我前面 ⇒ 任务实际等待 = 排队 + 执行，
    而老实现只有**一个**预算（600 次轮询），排队长一点就误报「超时」。

    本用例用假网关把「排队 700 次」演出来，然后断言：
      ① 老口径（单一预算 600）**失败** —— 证明这个场景老实现扛不住；
      ② 新口径（排队段预算更大）**通过** —— 证明修复真的解了这个场景。
    """
    handler, _ = _fake_gateway_after(worker_picks_up_at=700)
    auth = {"Authorization": "Bearer fake"}

    # ① 老口径：单一预算 600 次（= 原来那个拍脑袋的常数）
    with pytest.raises(pytest.fail.Exception, match="等待超时"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://fake"
        ) as client:
            await _poll_task_status(
                client,
                "dst_fake",
                auth,
                "art_fake",
                queue_budget_polls=600,
                exec_budget_polls=600,
                poll_interval_s=0,
            )

    # ② 新口径：排队段与执行段分开算 ⇒ 排 700 次也能等到结果
    handler, _ = _fake_gateway_after(worker_picks_up_at=700)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake"
    ) as client:
        data = await _poll_task_status(
            client,
            "dst_fake",
            auth,
            "art_fake",
            poll_interval_s=0,
        )
    assert data["status"] == "ready", data


@pytest.mark.asyncio
async def test_poll_reports_queue_phase_vs_running_phase():
    """超时时要说清是「还在排队」还是「已经起跑但没跑完」。

    为什么：这两个是完全不同的故障（前者是队列积压/环境问题，后者是链路坏了），
    混成一句「超时」就还得再查一轮 —— 而这正是 2026-10-25 定位花时间的原因。
    """
    handler, _ = _fake_gateway_after(worker_picks_up_at=10_000)
    auth = {"Authorization": "Bearer fake"}

    # 一直排不上队 ⇒ 报「还在排队」
    with pytest.raises(pytest.fail.Exception, match="还在排队"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://fake"
        ) as client:
            await _poll_task_status(
                client,
                "dst_fake",
                auth,
                "art_fake",
                queue_budget_polls=5,
                exec_budget_polls=5,
                poll_interval_s=0,
            )

    # worker 取走了任务但一直没跑完 ⇒ 报「已经起跑但没跑完」
    handler, _ = _fake_gateway_after(worker_picks_up_at=2, completes=False)
    with pytest.raises(pytest.fail.Exception, match="已经起跑但没跑完"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://fake"
        ) as client:
            await _poll_task_status(
                client,
                "dst_fake",
                auth,
                "art_fake",
                queue_budget_polls=100,
                exec_budget_polls=5,
                poll_interval_s=0,
            )
