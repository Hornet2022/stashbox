"""E2E-04 剪藏 + 收藏 + 稍后听 + 删除：内容生命周期闭环。

客户视角：
  剪藏（capture） → 收藏（favorite） → 稍后听（snooze/later-listen） → 删除

这组用例全部走 **后端接口** 断言。理由：
1. 剪藏页面依赖真实微信回调，adb 没法模拟 D9 share intent，走接口才可靠。
2. 收藏/稍后听/删除的断言价值在「后端状态对不对」，不是「按钮在不在」。
3. 部分操作（如稍后听的 snooze_until 时间计算）在 UI 上无法直接读出，
   但接口返回值和 DB 行都是可精确断言的。

后端接口路径（全部走 api-gateway 8100）：
  POST /api/v1/callback/d9-add-article   剪藏（D9 回调）
  POST /api/v1/articles/{id}/favorite     收藏
  POST /api/v1/articles/{id}/unfavorite   取消收藏
  POST /api/v1/articles/{id}/snooze       稍后听（带可选 snooze_until）
  DELETE /api/v1/articles/{id}/snooze      取消稍后听
  GET  /api/v1/later-listens              稍后听列表
  DELETE /api/v1/articles/{id}            删除文章（硬删除）

设计决策：
  - 每个用例独立创建测试数据，不依赖前序用例。D9 创建文章时若配额不足则 skip，
    而不是让后续断言在 None 上崩溃。
  - 收藏/稍后听/删除用例优先使用已有文章，避免消耗配额。
"""

from __future__ import annotations

import os
import time

import pytest

GATEWAY = "http://127.0.0.1:8100"

# 用例专用的 article_id 后缀，跑完可按此清理
_E2E_SUFFIX = "e2e_capture_"


def _create_article_via_d9(owner_http, url_suffix: str = "", url: str | None = None):
    """通过 D9 回调创建一篇测试文章，返回 response。

    走 api-gateway 8100，不是直连 content-service。
    url 显式传入时用它（幂等测试必须复用同一个 URL）；
    不传则按时间戳造一个新的，避免用例之间撞 URL。
    """
    if url is None:
        ts = int(time.time() * 1000)
        url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}{ts}{url_suffix}"
    return owner_http.post(
        "/api/v1/callback/d9-add-article",
        json={"url": url, "source": "d9"},
    )


def _any_owned_article(db, user_id: int = 1):
    """取 user 1 的一篇已有文章；没有就 None。"""
    rows = db(
        "select id from articles where user_id = $1 and deleted_at is null limit 1",
        user_id,
    )
    return rows[0]["id"] if rows else None


def _purge_by_url(url: str):
    """硬删该 URL 造出来的文章及所有关联行。

    **为什么必须清理**：本组用例挂在 user 1 名下（真机正在用的账号），
    造出来的 `e2e_capture_*` 文章会直接出现在真机 App 的文章列表里。
    本轮 04 跑完留下 32 篇测试文章排在列表最前，导致 test_e2e_02
    「详情页显示已就绪」打开的是测试造的『蒸馏中』文章而失败 ——
    那个失败看着像产品 bug，其实是测试数据污染。清理是测试的责任。

    走 SQL 而非 `DELETE /api/v1/articles/{id}`：后者是业务端点，删除会顺带
    清音频文件，而本组造的 URL 都是假链接，本来就没有音频。

    删除顺序有讲究：先子表后父表，直接删 articles 会撞
    `distilled_articles_article_id_fkey` 外键约束。
    """
    import asyncio

    import asyncpg

    dsn = os.getenv(
        "STASHBOX_TEST_DSN", "postgresql://stashbox:stashbox_dev@localhost:5432/stashbox"
    )

    async def _run():
        conn = await asyncpg.connect(dsn)
        try:
            ids = await conn.fetch("select id from articles where url = $1", url)
            for r in ids:
                aid = r["id"]
                for sql in (
                    "delete from distilled_articles where article_id = $1",
                    "delete from feedback where article_id = $1",
                    "delete from feedback_v2 where article_id = $1",
                    "delete from listening_statuses where article_id = $1",
                    "delete from later_listens where article_id = $1",
                    "delete from articles where id = $1",
                ):
                    await conn.execute(sql, aid)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


def _write_quota(user_id: int, monthly_quota: int | None = None, quota_used: int | None = None):
    """直写 users 表的配额字段。

    刻意用 asyncpg 直连而不用 SQLAlchemy 的 AsyncSessionLocal：
    AsyncSession 持有连接池，池里的连接绑定在创建它的那个 event loop 上。
    每条用例新建 event loop 去复用这个池，第二次就炸
    `got Future ... attached to a different loop` / `Event loop is closed`。
    之前 test_quota_exceeded_blocks_d9_callback 的 finally 恢复就是这么失败的 ——
    配额打满后恢复不回来，后面所有要配额的用例连锁 skip。
    """
    import asyncio

    import asyncpg

    dsn = os.getenv(
        "STASHBOX_TEST_DSN", "postgresql://stashbox:stashbox_dev@localhost:5432/stashbox"
    )

    sets, args = [], []
    if monthly_quota is not None:
        args.append(monthly_quota)
        sets.append(f"monthly_quota = ${len(args)}")
    if quota_used is not None:
        args.append(quota_used)
        sets.append(f"quota_used = ${len(args)}")
    if not sets:
        return
    args.append(user_id)
    sql = f"update users set {', '.join(sets)} where id = ${len(args)}"

    async def _run():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql, *args)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


def _ensure_quota(db, user_id: int = 1, min_remaining: int = 5):
    """确保用户有足够配额，不够就把 quota_used 清零。

    返回 (monthly_quota, quota_used) 原值供恢复；无需重置时返回 None。
    """
    rows = db("select monthly_quota, quota_used from users where id = $1", user_id)
    if not rows:
        return None
    quota, used = rows[0]["monthly_quota"], rows[0]["quota_used"]
    if quota - used < min_remaining and quota >= min_remaining:
        _write_quota(user_id, quota_used=0)
        return (quota, used)
    return None  # 无需重置


def _restore_quota(original, user_id: int = 1):
    """恢复配额到原始值。"""
    if original is None:
        return
    quota, used = original
    _write_quota(user_id, monthly_quota=quota, quota_used=used)


def test_d9_clip_charges_exactly_once_across_services(owner_http, db):
    """**回归**：剪藏一篇文章，全链路只扣 1 次配额。

    CP-AI-CHARGE-DUP + CP-DUPLICATE-CLIP 两条修复的联合回归。

    修复前的双重扣费（一次剪藏扣 2 次）是跨两个服务的：

      content-service d9_add_article
        1. quota_service.consume()          ← 扣 1
        2. cache_service.mark_article_quota(art.id)   ← 打 Redis 标
        3. trigger_distill() → ai-service
             ai-service distill_article
               existed_da = select ... where article_id = ...   ← 此刻还没有行
               already_charged = (existed_da is not None)      ← False
               quota_service.consume()          ← **又扣 1**

    content-service 打的「已扣配额」Redis 标，ai-service 从来没读过 ——
    `has_article_quota()` 写了却全项目零调用。免费档 5 篇/月，
    实际只能剪藏 2.5 篇，而且没有任何报错。

    修复：ai-service 的判据加上 Redis 标（`existed_da is not None or marked`）。
    """
    original = _ensure_quota(db)
    try:
        rows = db("select quota_used, monthly_quota from users where id = 1")
        used_before = rows[0]["quota_used"]
        if rows[0]["monthly_quota"] - used_before < 1:
            pytest.skip("配额不足")

        clip_url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}charge_once_{int(time.time())}"
        r = _create_article_via_d9(owner_http, url=clip_url)
        if r.status_code == 403:
            pytest.skip("配额被拒")
        assert r.status_code == 200, f"剪藏失败: {r.status_code} {r.text[:200]}"

        # ai-service 蒸馏是异步的，等它把配额也处理完
        deadline = time.time() + 15
        used_after = used_before
        while time.time() < deadline:
            used_after = db("select quota_used from users where id = 1")[0]["quota_used"]
            if used_after >= used_before + 1:
                break
            time.sleep(1)

        assert used_after == used_before + 1, (
            f"剪藏 1 次应只扣 1 个配额，实际扣了 {used_after - used_before} 个"
            f"（{used_before} → {used_after}）。"
            f"若为 +2，说明 content-service 和 ai-service 各扣了一次 —— "
            f"检查 ai-service 的 already_charged 是否漏了 cache_service.has_article_quota()。"
        )
    finally:
        _restore_quota(original)
        _purge_by_url(clip_url)


def test_d9_callback_creates_article_and_consumes_quota(owner_http, db):
    """D9 剪藏回调：创建文章 + 扣配额。

    这是整条客户动线的起点。断言：
    1. 返回 200 + article_id
    2. DB 里文章确实存在
    3. 配额 quota_used 确实 +1 了
    """
    original = _ensure_quota(db)
    try:
        rows = db("select quota_used, monthly_quota from users where id = 1")
        assert rows, "user 1 不存在"
        used_before = rows[0]["quota_used"]
        remaining = rows[0]["monthly_quota"] - used_before

        if remaining <= 0:
            pytest.skip("user 1 配额已满（即使重置后也不够），无法测试 D9 剪藏扣减")

        clip_url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}clip_{int(time.time())}"
        r = _create_article_via_d9(owner_http, url=clip_url)
        if r.status_code == 403:
            code = r.json().get("code")
            if code == 3001:
                pytest.skip("D9 剪藏被配额拒绝，跳过（可能其他用例消耗了配额）")
        assert r.status_code == 200, f"D9 回调失败: {r.status_code} {r.text[:300]}"
        body = r.json()
        article_id = body.get("article_id") or body.get("id")
        assert article_id, f"返回里没有 article_id: {body}"

        # DB 里文章确实存在
        art_rows = db("select id, source from articles where id = $1", article_id)
        assert art_rows, f"文章 {article_id} 在 DB 里找不到"
        assert art_rows[0]["source"] == "d9", f"source 应为 d9，实际 {art_rows[0]['source']}"

        # 配额 +1
        rows_after = db("select quota_used from users where id = 1")
        assert rows_after, "user 1 查不到了"
        assert (
            rows_after[0]["quota_used"] == used_before + 1
        ), f"配额未扣减: before={used_before}, after={rows_after[0]['quota_used']}"
    finally:
        _restore_quota(original)
        _purge_by_url(clip_url)


def test_d9_callback_idempotent_same_url(owner_http, db):
    """D9 剪藏：同一 URL 重复提交不应重复创建文章也不应重复扣配额。

    这是客户实际会遇到的场景：微信里重复分享同一篇文章。
    """
    original = _ensure_quota(db, min_remaining=2)
    try:
        rows = db("select quota_used, monthly_quota from users where id = 1")
        used_before = rows[0]["quota_used"]
        remaining = rows[0]["monthly_quota"] - used_before

        if remaining <= 1:
            pytest.skip("user 1 配额不够（至少需要 2 次余量），跳过幂等测试")

        # 关键：两次请求必须用**同一个 URL**，否则测的是"两次不同剪藏"而不是幂等。
        # 第一版这里每次生成时间戳 URL，断言永远不成立（拿到两个不同 article_id）。
        same_url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}idem_{int(time.time())}"

        # 第一次
        r1 = _create_article_via_d9(owner_http, url=same_url)
        if r1.status_code == 403:
            pytest.skip("配额已满，跳过幂等测试")
        assert r1.status_code == 200
        body1 = r1.json()
        aid1 = body1.get("article_id") or body1.get("id")

        # 同一 URL 再提交一次
        r2 = _create_article_via_d9(owner_http, url=same_url)
        # 无论返回 200（幂等）还是 409（冲突），都不应再创建新文章
        if r2.status_code == 200:
            body2 = r2.json()
            aid2 = body2.get("article_id") or body2.get("id")
            assert aid2 == aid1, f"幂等提交应返回同一 article_id: {aid1} vs {aid2}"
        elif r2.status_code == 409:
            pass  # 冲突，合理
        elif r2.status_code == 403 and r2.json().get("code") == 3001:
            pytest.fail("幂等提交被配额拒绝，说明第一次 D9 没有正确幂等")
        else:
            pytest.fail(f"幂等提交返回了意外状态码 {r2.status_code}: {r2.text[:200]}")

        # 配额只应 +1
        rows_after = db("select quota_used from users where id = 1")
        assert (
            rows_after[0]["quota_used"] == used_before + 1
        ), f"幂等提交重复扣配额: before={used_before}, after={rows_after[0]['quota_used']}"

        # 库里也只能有一行 —— 返回同一 id 并不保证没建重复行
        dup_rows = db(
            "select count(*) n from articles where user_id = 1 and url = $1 "
            "and deleted_at is null",
            same_url,
        )
        assert dup_rows[0]["n"] == 1, (
            f"同一 URL 在库里应有且仅有 1 行，实际 {dup_rows[0]['n']} 行。"
            f"articles 表对 (user_id, url) 没有唯一约束，"
            f"重复剪藏会静默产生重复条目。"
        )
    finally:
        _restore_quota(original)
        _purge_by_url(same_url)


def test_favorite_and_unfavorite(owner_http, db):
    """收藏 → 取消收藏：articles.favorite 字段 + feedback 行。

    收藏和稍后听是用户最常见的「稍后处理」动作。
    如果 favorite 没落库，收藏列表就是空的 —— 静默失灵。
    """
    # 优先使用已有文章，不额外消耗配额
    aid = _any_owned_article(db)
    if not aid:
        r = _create_article_via_d9(owner_http, url_suffix="_fav")
        if r.status_code == 403:
            pytest.skip("配额已满且无已有文章，跳过")
        assert r.status_code == 200, f"创建文章失败: {r.status_code}"
        aid = r.json().get("article_id") or r.json().get("id")

    # 先取消收藏确保干净状态
    owner_http.post(f"/api/v1/articles/{aid}/unfavorite")

    # 收藏
    r_fav = owner_http.post(f"/api/v1/articles/{aid}/favorite")
    assert r_fav.status_code == 200, f"收藏失败: {r_fav.status_code} {r_fav.text[:200]}"
    assert r_fav.json().get("favorite") is True, f"收藏响应 favorite 不为 True: {r_fav.json()}"

    # DB 确认
    rows = db("select favorite from articles where id = $1", aid)
    assert rows and rows[0]["favorite"] is True, "articles.favorite 没变成 True"

    # 取消收藏
    r_unfav = owner_http.post(f"/api/v1/articles/{aid}/unfavorite")
    assert r_unfav.status_code == 200, f"取消收藏失败: {r_unfav.status_code} {r_unfav.text[:200]}"

    rows = db("select favorite from articles where id = $1", aid)
    assert rows and rows[0]["favorite"] is False, "取消收藏后 favorite 不为 False"


def test_snooze_and_later_listens(owner_http, db):
    """稍后听：snooze → 出现在 later-listens → 取消 snooze → 消失。

    稍后听是「通勤收听」的核心入口。如果 snooze 写不进 DB，
    later-listens 就为空，用户以为功能坏了。
    """
    aid = _any_owned_article(db)
    if not aid:
        r = _create_article_via_d9(owner_http, url_suffix="_snooze")
        if r.status_code == 403:
            pytest.skip("配额已满且无已有文章，跳过")
        assert r.status_code == 200
        aid = r.json().get("article_id") or r.json().get("id")

    # 先清除 snooze 状态
    owner_http.delete(f"/api/v1/articles/{aid}/snooze")

    # snooze
    r_snooze = owner_http.post(
        f"/api/v1/articles/{aid}/snooze",
        json={},
    )
    assert r_snooze.status_code == 200, f"snooze 失败: {r_snooze.status_code} {r_snooze.text[:200]}"

    # later-listens 里应该有
    r_list = owner_http.get("/api/v1/later-listens")
    assert r_list.status_code == 200
    body = r_list.json()
    items = body.get("later_listens") if isinstance(body, dict) else body
    if isinstance(items, dict):
        items = items.get("items", items.get("later_listens", []))
    ids = [i.get("article_id") if isinstance(i, dict) else i for i in items]
    assert aid in ids, f"snooze 后 {aid} 不在 later-listens 里: ids={ids[:5]}"

    # 取消 snooze
    r_unsnooze = owner_http.delete(f"/api/v1/articles/{aid}/snooze")
    assert r_unsnooze.status_code == 200, f"取消 snooze 失败: {r_unsnooze.status_code}"

    # later-listens 里应该没了
    r_list2 = owner_http.get("/api/v1/later-listens")
    body2 = r_list2.json()
    items2 = body2.get("later_listens") if isinstance(body2, dict) else body2
    if isinstance(items2, dict):
        items2 = items2.get("items", items2.get("later_listens", []))
    ids2 = [i.get("article_id") if isinstance(i, dict) else i for i in items2]
    assert aid not in ids2, f"取消 snooze 后 {aid} 仍在 later-listens 里"


def test_snooze_with_until_persists_time(owner_http, db):
    """snooze 带指定时间：snooze_until 要真的写进 DB。

    客户场景：「提醒我明天听」→ 客户端传 snooze_until。
    如果这个字段没落库，"明天提醒我"就是假的 —— 永远不会提醒。
    """
    aid = _any_owned_article(db)
    if not aid:
        r = _create_article_via_d9(owner_http, url_suffix="_snooze_until")
        if r.status_code == 403:
            pytest.skip("配额已满且无已有文章，跳过")
        assert r.status_code == 200
        aid = r.json().get("article_id") or r.json().get("id")

    # 先清除 snooze 状态
    owner_http.delete(f"/api/v1/articles/{aid}/snooze")

    # 带 snooze_until
    from datetime import datetime, timezone, timedelta

    tomorrow = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    r_snooze = owner_http.post(
        f"/api/v1/articles/{aid}/snooze",
        json={"snooze_until": tomorrow},
    )
    assert (
        r_snooze.status_code == 200
    ), f"snooze(until) 失败: {r_snooze.status_code} {r_snooze.text[:200]}"

    # DB 确认 snooze_until 非空
    rows = db(
        "select snooze_until from later_listens where article_id = $1 and user_id = 1",
        aid,
    )
    assert rows, f"later_listens 里没有 {aid} 的行"
    assert rows[0]["snooze_until"] is not None, "snooze_until 为 NULL，指定时间没落库"

    # 清理
    owner_http.delete(f"/api/v1/articles/{aid}/snooze")


def test_delete_article_cleans_up(owner_http, db):
    """删除文章：文章行消失 + 关联数据清理。

    CP-DELETE 的回归。删了文章但蒸馏行、收藏行还在 →
    列表查询会 JOIN 到幽灵数据。

    必须用 D9 创建的文章来测（因为需要确认硬删除真的清了），
    所以会消耗一个配额。
    """
    original = _ensure_quota(db)
    try:
        rows_q = db("select monthly_quota, quota_used from users where id = 1")
        remaining = rows_q[0]["monthly_quota"] - rows_q[0]["quota_used"]
        if remaining <= 0:
            pytest.skip("配额已满，无法创建测试文章来测删除")

        clip_url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}delete_{int(time.time())}"
        r = _create_article_via_d9(owner_http, url=clip_url)
        if r.status_code == 403:
            pytest.skip("D9 创建被配额拒绝，跳过删除测试")
        assert r.status_code == 200, f"创建文章失败: {r.status_code}"
        aid = r.json().get("article_id") or r.json().get("id")

        # 先收藏 + snooze，造出关联数据
        owner_http.post(f"/api/v1/articles/{aid}/favorite")
        owner_http.post(f"/api/v1/articles/{aid}/snooze", json={})

        # 确认关联行存在。
        # 注意写进的是 `feedback` 表（有 type 列），不是 `feedback_v2`
        # （那个表列名是 category，且收藏走不到那里）。
        fav_rows = db(
            "select id from feedback where article_id = $1 and type = 'favorite'",
            aid,
        )
        ll_rows = db("select id from later_listens where article_id = $1", aid)
        assert fav_rows, "收藏 feedback_v2 行应在"
        assert ll_rows, "later_listens 行应在"

        # 删除
        r_del = owner_http.delete(f"/api/v1/articles/{aid}")
        assert r_del.status_code == 200, f"删除失败: {r_del.status_code} {r_del.text[:200]}"

        # 文章行消失（硬删除）
        art_rows = db("select id from articles where id = $1", aid)
        assert not art_rows, f"删除后 articles 行仍存在: {aid}"

        # later_listens 关联行也清理了
        ll_after = db("select id from later_listens where article_id = $1", aid)
        assert not ll_after, f"删除后 later_listens 行仍在: {aid}"
    finally:
        _restore_quota(original)
        _purge_by_url(clip_url)


def test_non_owner_cannot_delete_others_article(http, db):
    """非属主不能删除别人的文章（越权回归）。

    CP-DELETE：删除是破坏性操作，按「越权 404」决策 ——
    不泄露资源存在性，返回 404 而非 403。
    """
    # user 1 的文章
    rows = db("select a.id from articles a where a.user_id = 1 and a.deleted_at is null limit 1")
    if not rows:
        pytest.skip("user 1 没有文章")
    aid = rows[0]["id"]

    # admin (user 9018) 尝试删除
    r = http.delete(f"/api/v1/articles/{aid}")
    assert (
        r.status_code == 404
    ), f"非属主删除应返回 404（越权不泄露），实际 {r.status_code}: {r.text[:200]}"

    # 文章还在
    art_rows = db("select id from articles where id = $1", aid)
    assert art_rows, f"非属主删除后文章消失了: {aid}"


def test_quota_exceeded_blocks_d9_callback(owner_http, db):
    """配额耗尽后 D9 剪藏被拒绝。

    这条和 test_e2e_03_quota 的服务层断言互补 ——
    那条锁的是 quota_service.consume() 抛 BizException，
    这条锁的是 D9 回调端到端真的会拒绝。
    """
    rows = db("select monthly_quota, quota_used from users where id = 1")
    if not rows:
        pytest.skip("user 1 不存在")
    original_quota, original_used = rows[0]["monthly_quota"], rows[0]["quota_used"]

    # 把配额打满
    _write_quota(1, monthly_quota=original_quota, quota_used=original_quota)

    try:
        block_url = f"https://mp.weixin.qq.com/s/{_E2E_SUFFIX}block_{int(time.time())}"
        r = _create_article_via_d9(owner_http, url=block_url)
        # 应被拒绝：403（业务码 3001）
        assert (
            r.status_code == 403
        ), f"配额耗尽后 D9 回调应 403，实际 {r.status_code}: {r.text[:200]}"
        body = r.json()
        assert body.get("code") == 3001, (
            f"配额耗尽的业务码应为 3001，实际 {body}。"
            f"若改为其他码，Android 的 isQuotaExceeded 判断会失配，付费墙不弹。"
        )
    finally:
        # 恢复配额。必须在 finally 里无条件恢复 —— 这条把配额打满了，
        # 恢复失败会让后面所有需要配额的用例连锁 skip。
        _write_quota(1, monthly_quota=original_quota, quota_used=original_used)
        _purge_by_url(block_url)
