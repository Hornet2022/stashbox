"""E2E-03 账户配额：字段契约、接口与库一致、配额耗尽的错误契约。

客户视角：我想知道"我这个月还能用多少次"；用完了会怎样。

这组用例全部**不需要设备** —— 它们锁的是接口契约。
设备侧的行为（付费墙弹不弹、数据显示对不对）见 test_e2e_02。

为什么配额契约值得单独锁死：这是本项目唯一有真实计费语义的机制，
此前的断点也最隐蔽 —— 扣减那半边是通的，但用尽之后整条路是断的，
而且**断得没有任何报错**：

  后端 `QuotaExceededError.http_status = 403`
  App   `isQuotaExceeded` 判 `httpCode == 429 && bizCode == 3001`
                                  ↑ 429 从来不会发生 → 付费墙永远不弹

⚠ 本组会临时修改 users 表的配额，务必复原（每条用例都有 finally）。
"""

from __future__ import annotations

import os

import pytest

_DSN = os.getenv("STASHBOX_TEST_DSN", "postgresql://stashbox:stashbox_dev@localhost:5432/stashbox")


def _exec_sql(sql: str, *args):
    """asyncpg 直连跑一条写 SQL。

    刻意不用 SQLAlchemy：AsyncSession 的连接池绑定在创建它的 event loop 上，
    跨 loop 复用会抛 `got Future ... attached to a different loop`。
    """
    import asyncio

    import asyncpg

    async def _run():
        conn = await asyncpg.connect(_DSN)
        try:
            await conn.execute(sql, *args)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


def _set_distill_status(article_id: str, status: str):
    _exec_sql("update distilled_articles set status = $2 where article_id = $1", article_id, status)


def _set_article_status(article_id: str, status: str):
    _exec_sql("update articles set status = $2 where id = $1", article_id, status)


async def _consume(uid: int):
    """跑一次 quota_service.consume，异常向上抛给调用方。"""
    from stashbox.backend.common import quota_service
    from stashbox.backend.common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await quota_service.consume(session, uid)


def test_quota_endpoint_returns_fields_app_expects(http):
    """GET /users/me/quota 的字段名必须和 Android 模型对得上。

    CP-QUOTA-FIELD 的回归。此前 App 读 `used_quota`、后端返 `quota_used`，
    字段名对不上 → usedQuota 恒 null → 付费墙「已用」永远显示 0。
    这种 bug 单元测试抓不到（两边各自都"正确"），只有对着真实响应才看得见。
    """
    r = http.get("/api/v1/users/me/quota")
    assert r.status_code == 200, f"取配额失败: {r.status_code} {r.text[:200]}"
    body = r.json()

    # Android QuotaResponse 声明的字段（修复后的口径）
    for field in ("monthly_quota", "quota_used", "remaining"):
        assert field in body, f"响应缺少 Android 依赖的字段 {field}: {body}"

    # 反向钉死：后端不得再返 used_quota（那是 App 曾经读错的名字）
    assert (
        "used_quota" not in body
    ), f"后端仍在返 used_quota，App 读的是 quota_used，字段名仍对不上: {body}"


def test_quota_values_match_database(http, db):
    """接口返回的配额数字和库里一致。

    配额走 Redis 缓存（`_quota_payload`），这里顺带验证缓存没返回脏值。
    """
    body = http.get("/api/v1/users/me/quota").json()
    rows = db("select monthly_quota, quota_used from users where id = 9018")
    assert rows, "user 9018 不存在"
    u = rows[0]
    assert (
        body["monthly_quota"] == u["monthly_quota"]
    ), f"monthly_quota 不一致: 接口 {body['monthly_quota']} vs 库 {u['monthly_quota']}"
    assert (
        body["quota_used"] == u["quota_used"]
    ), f"quota_used 不一致: 接口 {body['quota_used']} vs 库 {u['quota_used']}"
    assert (
        body["remaining"] == u["monthly_quota"] - u["quota_used"]
    ), f"remaining 算错: {body['remaining']}（应为 {u['monthly_quota'] - u['quota_used']}）"


def test_quota_exceeded_contract_is_403_and_3001(db):
    """配额耗尽的错误契约：code=3001，http_status=**403**。

    这是本组最关键的一条。它把"后端到底返 403 还是 429"钉死 ——
    以后改哪一边都行，但两边必须同时改，否则付费墙又会静默失灵。

    直接调 quota_service 而不走 HTTP：这样能同时锁住 code 和 http_status，
    不受网关/路由层包装影响。跑完立即回滚，不留痕。

    改配额走 asyncpg 直连而非 SQLAlchemy：AsyncSession 的连接池绑定在
    创建它的 event loop 上，跨 loop 复用会抛
    `got Future ... attached to a different loop`，恢复不回来。
    """
    import asyncio

    import asyncpg

    from stashbox.backend.common.exceptions import BizException

    dsn = _DSN
    TARGET_UID = 9277

    async def _set_quota(uid, monthly, used):
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "update users set monthly_quota = $2, quota_used = $3 where id = $1",
                uid,
                monthly,
                used,
            )
        finally:
            await conn.close()

    orig = db("select monthly_quota, quota_used from users where id = $1", TARGET_UID)
    if not orig:
        pytest.skip(f"user {TARGET_UID} 不存在")
    original = (orig[0]["monthly_quota"], orig[0]["quota_used"])

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_set_quota(TARGET_UID, original[0], original[0]))
        try:
            quota_service_result = loop.run_until_complete(_consume(TARGET_UID))
        except BizException as exc:
            quota_service_result = (exc.code, exc.http_status)
    finally:
        loop.run_until_complete(_set_quota(TARGET_UID, *original))
        loop.close()

    assert quota_service_result is not None, "配额打满后 consume 竟然没抛异常"
    code, http_status = quota_service_result
    assert code == 3001, f"业务码应为 3001，实际 {code}"
    assert http_status == 403, (
        f"配额耗尽的 HTTP 状态码是 {http_status}，不是 403。"
        f"Android 的 isQuotaExceeded 若按 429 判断，付费墙将永远不弹。"
    )


def test_distill_by_non_owner_rejected(http):
    """非属主不能触发他人文章的蒸馏（越权回归）。

    CP-DISTILL-START-HARDEN 的一部分。此前 `/api/v1/distill/start` 只信请求体里的
    article_id，从不查归属 —— 实测以 user 9277 的身份对 user 1 名下的文章
    调用返回 200。现在两个端点都要求属主。
    """
    r = http.post(
        "/api/v1/distill/start",
        json={
            "article_id": "art_wxSOP_verify_0001",  # 属主是 user 1
            "url": "https://example.com/evil",
        },
    )
    assert r.status_code == 403, f"非属主触发蒸馏应 403，实际 {r.status_code}: {r.text[:200]}"
    assert (
        "owner" in r.text.lower() or r.json().get("code") == 40300
    ), f"403 的错误信息应说明是归属问题: {r.text[:200]}"


def test_retrigger_distill_preserves_existing_artifact(owner_http, db):
    """重跑蒸馏**不会**打掉已有产物。

    CP-DISTILL-NONDESTRUCTIVE 的回归。原实现复用已有行时把
    script_text / audio_url / duration_sec / quality_score / tags 一律置空，
    本轮验收就因为这个把真机在用的一篇 done 文章打成了 queued，元数据全丢。

    断言口径：调完之后 status 变 queued 是对的（确实在重跑），
    但 **audio_url / script_text 必须还在** —— 旧产物要保留到新产物就位。

    数据自建，**不碰真机在用的文章**。原先直接拿 `art_wxSOP_verify_0001`
    （user 1 唯一一篇、真机正在用）开跑，问题有两个：
      1) `/distill/start` 会 enqueue 一个 arq job，worker 异步跑完把状态写成
         failed —— 测试的 finally 恢复发生在 worker 之前，恢复被覆盖，
         真机上那篇就变成「蒸馏失败」。
      2) 跑一次就留下一篇 failed 的真机数据，污染被测环境。
    改成自己造一篇带产物的文章，测完连同蒸馏行一起删掉。
    """
    import time

    url = f"https://mp.weixin.qq.com/s/e2e_retrigger_{int(time.time())}"
    r = owner_http.post("/api/v1/callback/d9-add-article", json={"url": url, "source": "d9"})
    if r.status_code == 403:
        pytest.skip("配额不足，无法造测试文章")
    assert r.status_code == 200, f"造文章失败: {r.status_code} {r.text[:200]}"
    aid = r.json().get("article_id") or r.json().get("id")

    # 伪造一条"已完成的产物"：直接写 distilled_articles，不等真 TTS。
    # 这条测的是**重跑时旧产物保不保得住**，不需要产物是真的音频。
    _exec_sql(
        "delete from distilled_articles where article_id = $1", aid
    )  # 清掉 D9 刚触发的排队任务
    _exec_sql(
        "insert into distilled_articles (id, article_id, status, audio_url, script_text) "
        "values ($1, $2, 'done', $3, $4) "
        "on conflict (article_id) do update set status='done', audio_url=excluded.audio_url, "
        "script_text=excluded.script_text",
        f"dst_e2e_{aid[-12:]}",
        aid,
        f"http://127.0.0.1:8333/stashbox-audio/audio/{aid}.wav",
        "这是旧产物的正文，应该在重跑后依然存在。",
    )

    try:
        before = db(
            "select id, status, audio_url, length(script_text) script_len "
            "from distilled_articles where article_id = $1",
            aid,
        )[0]
        assert before["audio_url"] is not None, "前置数据没造上"

        r = owner_http.post(
            "/api/v1/distill/start",
            json={"article_id": aid, "url": "https://example.com/placeholder"},
        )
        assert r.status_code == 200, f"属主重跑应 200，实际 {r.status_code}: {r.text[:200]}"

        after = db(
            "select status, audio_url, length(script_text) script_len "
            "from distilled_articles where article_id = $1",
            aid,
        )[0]

        assert after["status"] == "queued", f"重跑后 status 应为 queued，实际 {after['status']}"
        assert (
            after["audio_url"] is not None
        ), "重跑把 audio_url 清空了 —— 正在收听的用户会立刻失去音频"
        assert (
            after["script_len"] == before["script_len"]
        ), f"重跑把 script_text 清空了：{before['script_len']} → {after['script_len']}"
    finally:
        # 自己的数据自己清干净，不留 failed 残渣
        for sql in (
            "delete from distilled_articles where article_id = $1",
            "delete from feedback where article_id = $1",
            "delete from listening_statuses where article_id = $1",
            "delete from later_listens where article_id = $1",
            "delete from articles where id = $1",
        ):
            _exec_sql(sql, aid)
