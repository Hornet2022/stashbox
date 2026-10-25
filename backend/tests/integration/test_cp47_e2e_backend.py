"""
CP4.7-E2E-BACKEND 端到端集成测试（content-service → ai-service → audio_url）。

测试范围：
1. 完整链路：公众号 URL → 抓取 → 蒸馏 → audio_url
2. 配额不足 → 402/403
3. 不支持的 URL → 边界处理

注意：POST /api/v1/articles/{id}/distill 在 gateway 中通过 fallback
（第一段 "articles" → content-service）路由，但 content-service 无此端点。
实际使用 /api/v1/distill/start（显式路由到 ai-service）。
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


async def _poll_task_status(
    client: httpx.AsyncClient, task_id: str, auth: dict, timeout: int = 600
) -> dict:
    """轮询 GET /api/v1/distill/{task_id} 直到 status == 'ready'（最多 timeout 秒）。

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
    """
    for i in range(timeout):
        resp = await client.get(f"/api/v1/distill/{task_id}", headers=auth)
        assert resp.status_code == 200, f"task status poll failed: {resp.status_code} {resp.text}"
        data = resp.json()
        if data.get("status") == "ready":
            return data
        if data.get("status") in ("failed", "error"):
            pytest.fail(f"蒸馏任务失败: {data}")
        await asyncio.sleep(1)
    pytest.fail(f"任务 {task_id} 状态轮询超时：最后 status={data}")


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

        # Step 3: 轮询任务状态直到 ready（真实 LLM+TTS 流水线，最多 600s）
        # ⚠️ 对外状态是 "ready"（内部 done 经 common/distill_status.derive_distill_status 派生），
        # 不是内部状态机的 "done" —— 判据用错状态层会一路轮询到超时。
        task_data = await _poll_task_status(client, task_id, auth, timeout=600)
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


# ai-service 直连端口（gateway 路由 articles/* → content-service，
# 不走 /articles/{id}/distill 故需直打 ai-service）
def _ai_service_url() -> str:
    """ai-service 直连地址 —— 从网关地址推导，**不写死端口**。

    为什么必须推导：这条用例要直打 ai-service（`/articles/{id}/distill` 在
    gateway 里 fallback 到 content-service，那边没这个端点）。原来这里写死
    `http://localhost:8103`，而 8103 是**生产** ai-service 的端口，且它真的在
    监听（本机 lsof 实测）。于是把 STASHBOX_GATEWAY 指向 e2e 网关（:18100）
    跑这套用例时，会发生最坏的一种组合：

        用户/文章建在**隔离库**（stashbox_e2e）
        蒸馏任务却入队到**生产** ai-service

    —— 用例在"隔离环境"跑，却往生产队列派活、占 TTS、写生产库。
    和 e2e-backend.sh 注释里记的那次事故同一类（那次是网关上游没全指到 e2e
    实例，音色写进了生产 bucket）：静默默认值比没有默认值危险。

    推导规则：两套环境网关与 ai-service 的端口差都是 +3
    （生产 8100→8103、e2e 18100→18103），所以从网关端口 +3 即可。
    需要覆盖时显式设 STASHBOX_AI_SERVICE。
    """
    override = os.getenv("STASHBOX_AI_SERVICE")
    if override:
        return override
    parsed = urlparse(GATEWAY_URL)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port + 3}"


AI_SERVICE_URL = _ai_service_url()


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

    另注：`/articles/{id}/distill` 在 gateway 里显式路由到 ai-service
    （不是 fallback），所以这里直打 ai-service 是为了看 job_id。
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

    async with httpx.AsyncClient(base_url=AI_SERVICE_URL, timeout=60.0) as ai_client:
        # 等自动派的那次蒸馏跑完 —— 否则幂等守卫会吞掉 simulate_failure
        assert auto_task_id, "创建响应里没有 task_id，无法等自动蒸馏"
        await _poll_task_status(ai_client, auto_task_id, auth, timeout=900)

        # 现在再请求「模拟失败」：守卫不该命中（已有任务是 done）
        distill_resp = await ai_client.post(
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
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as gw_client:
            for _ in range(180):
                st = await gw_client.get(f"/api/v1/articles/{article_id}/status", headers=auth)
                assert st.status_code == 200, f"文章状态查询失败: {st.status_code} {st.text}"
                status_data = st.json()
                if status_data.get("status") == "failed":
                    break
                await asyncio.sleep(1)
            else:
                pytest.fail(f"文章状态没有变成 failed: {status_data}")

    # 失败 ⇒ 配额退还（剪藏时扣的那 1 次要退回来）
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        quota_after = (await client.get("/api/v1/users/me/quota", headers=auth)).json()
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
