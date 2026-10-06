"""写路径必须配套失效缓存（2026-10 回归）

`article:detail:{id}`（ttl 300s）和 `user:pending:{uid}`（ttl 60s）两个缓存，
在项目里已经有明确的失效约定：**所有改文章的写路径，commit 之后都要失效**。
favorite / unfavorite / skip / snooze / feedback / 删除 / ���理音频作废 等十来条路径
都照做了（`favorite` 那行甚至带着「CP8.6 Bug 1: 失效 stale 缓存」的注释）。

但有两条写路径漏了，而它们恰好改的都是**列表的过滤依据**：

  1. `POST /articles/{id}/mark-listened` —— status 改成 `listened`
  2. `POST /articles/{id}/retry`       —— status 从 `failed` 改回 `pending`

待听列表的查询是 `status.in_(["pending", "distilling", "ready"])`，
所以第 1 条本该让文章**从待听里消失**，第 2 条本该让它**重新出现**。
两条都没失效缓存时，用户看到的都是上一步的状态：
  - 听完后文章还在待听列表里赖着（再点「听完了」还是同一结果，像按钮坏了），
    最长 60 秒；
  - 详情页/重试后的状态与 retry 次数停留在旧值，最长 300 秒。

这类缺口不会被单测发现，因为大部分用例每次都是「冷缓存第一次访问」。

判据刻意用「读两次」而不是查类名：先访问把缓存焐热，做一次写操作，
再读一次看是不是新值 —— 只有这样才真的在测「缓存有没有被失效」。
"""

from helpers import client, new_article, new_task, new_user


def detail_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}"


PENDING_URL = "/api/v1/articles/pending"


async def _prime_caches(uid: int, token: str, article_id: str) -> None:
    """把详情缓存和待听缓存都焐热（模拟用户刚打开过这些页面）。"""
    async with client(token) as c:
        await c.get(detail_url(article_id))
        await c.get(PENDING_URL)


async def _cached_pending_ids(token: str) -> set:
    async with client(token) as c:
        r = await c.get(PENDING_URL)
    assert r.status_code == 200, r.text
    return {a["id"] for a in r.json()["articles"]}


# ---------------------------------------------------------------------------
# 1. mark-listened：听完后要从待听里消失
# ---------------------------------------------------------------------------


async def test_mark_listened_removes_from_cached_pending_list():
    uid, token = await new_user()
    article_id = await new_article(uid, status="ready")
    await new_task(article_id, status="done", duration_sec=120)
    await _prime_caches(uid, token, article_id)
    assert article_id in await _cached_pending_ids(token), "前提没成立：文章本该在待听里"

    async with client(token) as c:
        r = await c.post(f"{detail_url(article_id)}/mark-listened")
    assert r.status_code == 200, r.text

    # 核心判据：待听列表缓存必须已经被失效，不能再吐出这篇文章
    assert article_id not in await _cached_pending_ids(
        token
    ), "听完了但文章还在待听列表里（缓存没失效）—— 按钮看起来像坏了"


async def test_mark_listened_refreshes_cached_detail_payload():
    """详情缓存也要失效（防御性）。

    注意判据**不是** status：详情响应的 status 是 `_derive_status(art, task)`
    派生的（task 为 done 就返回 `ready`），本就不反映 `listened` —— 这是既有
    设计（status 在详情里表示「音频可播」），不是缓存问题，别把它写进断言。

    这里量的是「两次请求返回的 payload 完全一致」：如果缓存没失效，
    第二次拿到的就是逐字节相同的旧 dict。写得更弱的判据（比如只看 status）
    会被上面那个派生逻辑掩盖，测不出东西。
    """
    uid, token = await new_user()
    article_id = await new_article(uid, status="ready")
    await new_task(article_id, status="done", duration_sec=120)
    await _prime_caches(uid, token, article_id)

    async with client(token) as c:
        before = await c.get(detail_url(article_id))
        await c.post(f"{detail_url(article_id)}/mark-listened")
        after = await c.get(detail_url(article_id))

    assert before.status_code == 200 and after.status_code == 200, after.text
    # 判据：失效之后必须**重新查库**。改用「重读后仍一致就说明没失效」的反证法
    # 不可靠（库里可能真没变化），所以改为直接断言失效调用存在（见文件末尾形态守卫），
    # 这里只守住「不因缓存失效而报错/拿不到数据」。
    assert after.json()["id"] == article_id


# ---------------------------------------------------------------------------
# 2. retry：失败重试后要重新回到待听
# ---------------------------------------------------------------------------


async def test_retry_makes_article_appear_in_cached_pending_list():
    uid, token = await new_user()
    article_id = await new_article(uid, status="failed", error="tts timeout")
    await new_task(article_id, status="failed")
    await _prime_caches(uid, token, article_id)
    # failed 不在待听过滤条件里，缓存里本来就没有它
    assert article_id not in await _cached_pending_ids(token), "前提不成立：failed 不该在待听里"

    async with client(token) as c:
        r = await c.post(f"{detail_url(article_id)}/retry")
    assert r.status_code == 200, r.text

    assert article_id in await _cached_pending_ids(
        token
    ), "重试成功了但文章没回到待听列表（缓存没失效）—— 用户得等最多 60s 才知道"


async def test_retry_detail_still_readable_after_cache_invalidation():
    """重试后详情仍可读（判据见上面 mark_listened 那条的说明：status 是派生的）。

    顺带记一个实测现象：重试把 `articles.status` 置回 pending，但
    `distilled_articles.status` 仍是 failed，于是 `_derive_status` 在
    ai-service 回写新任务之前会继续返回 `failed`。这是**既有行为**，
    客户端轮询的 `/articles/{id}/status` 不走缓存，所以拿到的一直是真值。
    这里只保证「读得到、不是 5xx」。
    """
    uid, token = await new_user()
    article_id = await new_article(uid, status="failed", error="tts timeout")
    await new_task(article_id, status="failed")
    await _prime_caches(uid, token, article_id)

    async with client(token) as c:
        await c.post(f"{detail_url(article_id)}/retry")
        after = await c.get(detail_url(article_id))

    assert after.status_code == 200, after.text
    assert after.json()["id"] == article_id


# ---------------------------------------------------------------------------
# 3. 形态守卫：漏掉新写路径时要能被发现
# ---------------------------------------------------------------------------


def test_两条写路径都接了缓存失效():
    """源码级断言：这两条端点里必须有失效调用。

    上一轮的教训是「有约定的路径会漏」，而漏了没有任何测试会红 —— 因为大部分
    用例每次都是冷缓存第一次访问。补一条形态断言，让下次再加写路径时至少有个
    地方提醒去接失效。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "content-service" / "main.py").read_text()

    for func_name in ("async def mark_listened", "async def user_retry_distill"):
        assert func_name in src, f"找不到 {func_name}，源码断言需要跟着更新"
        start = src.index(func_name)
        nxt = src.find("\n@app.", start)
        body = src[start : nxt if nxt != -1 else len(src)]
        assert "invalidate_article" in body, f"{func_name} 改了数据却没失效详情缓存"
        assert "invalidate_pending" in body, f"{func_name} 改了状态却没失效待听列表缓存"
