"""剪藏**后台抓取**（2026-11-11）的行为回归。

## 为什么要有这批用例

内测反馈（uid 23）：剪藏时反复弹「网络连接失败」，关掉 app 重开又正常。
取证结论：那几次剪藏**服务端全部 200、文章全部建好**，客户端却报错
⇒ 失败发生在客户端本地，根因是「剪藏接口是同步长请求」——
客户端要等服务端当场抓取（几秒到几十秒），期间任何客户端侧抖动
都变成「**服务端成功、客户端报错**」。

⇒ 剪藏接口改成**快速返回 + 后台抓取**。这带来一批**新的语义**，
每一条都必须有钉子（否则下一个人改回去，没有任何测试会红）：

1. 接口**立即返回**（文章行已建，正文还没有）；
2. 后台抓到 ⇒ 写正文 + **扣 1 次配额** + 派蒸馏；
3. 后台抓不到 ⇒ 文章 `status=failed` + **面向用户的中文原因**，且**不扣配额**
   （先抓后扣，2026-10-03 定的口径）；
4. 抓取中重复提交同一 URL ⇒ 同一篇、**不重复驱动**（否则抓两次扣两次）；
5. 抓取失败的文章再提交 ⇒ **重新驱动抓取**（不能永远返回失败）；
6. 进程重启留下的孤儿（pending 且没正文）⇒ 兜底定时器标记 failed；
7. 没有正文的文章**不许派蒸馏**（否则占位串被当正文喂给 LLM）。

## 依赖

真实 PG（5432）+ Redis（6379）+ `alembic upgrade head`。
抓取层整体 mock 掉（一个外网请求都不发）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from fetchers.base import FetcherError, FetcherErrorCode, FetchResult
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.exceptions import BizException
from stashbox.backend.common.models import Article

from helpers import (
    article_row,
    client,
    content_main,
    drain_capture_tasks,
    new_article,
    new_user,
    quota_used,
)

ARTICLES = "/api/v1/articles"

capture_pipeline = content_main.capture_pipeline


# ---------------------------------------------------------------------------
# 抓取层 mock
# ---------------------------------------------------------------------------
class _FakeFetcher:
    """可控的抓取替身：成功 / 失败两种结局。

    `error_code` / `error_source` 控制失败时抛的 `FetcherError` ——
    用于验证「后台抓取失败 → 文章 error 透传按 source 细分的文案」这条链路。
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        error_code=FetcherErrorCode.NETWORK,
        error_source="wechat_mp",
    ) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.delay: float = 0.0
        self.error_code = error_code
        self.error_source = error_source

    async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
        self.calls.append(url)
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        if self.fail:
            raise FetcherError(self.error_code, "boom", source=self.error_source)
        return FetchResult(
            url=url,
            title="测试文章",
            content_html="<p>正文</p>",
            content_text="这是测试用的正文内容。" * 40,
            source="wechat_mp",
        )


@pytest.fixture
def fake_fetcher(monkeypatch) -> _FakeFetcher:
    fetcher = _FakeFetcher()
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    return fetcher


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------
def test_查重决策_有正文就复用():
    assert (
        capture_pipeline.decide_existing_clip_action(
            status="pending", has_content=True, in_flight=False
        )
        == "reuse"
    )


def test_查重决策_没正文但正在抓就等着():
    assert (
        capture_pipeline.decide_existing_clip_action(
            status="pending", has_content=False, in_flight=True
        )
        == "await"
    )


def test_查重决策_没正文也没人在抓就重新抓():
    """失败过 / 进程重启留下的孤儿 ⇒ 重新驱动，不能永远复用失败的那篇。"""
    assert (
        capture_pipeline.decide_existing_clip_action(
            status="failed", has_content=False, in_flight=False
        )
        == "refetch"
    )


def test_超时判定_年龄未知算超时():
    """年龄算不出来（NULL）⇒ 保守处理：当孤儿收尾，别让它永远 pending。"""
    assert capture_pipeline.is_fetch_stale(None) is True


def test_超时判定_刚建的还算活着():
    assert capture_pipeline.is_fetch_stale(5) is False


def test_超时判定_很久以前的算孤儿():
    assert capture_pipeline.is_fetch_stale(3600) is True


def test_超时判定_阈值只有一处():
    """阈值是**一个常量**（FETCH_TIMEOUT_SECONDS），不是散落在各处的字面量。"""
    assert capture_pipeline.is_fetch_stale(capture_pipeline.FETCH_TIMEOUT_SECONDS + 1) is True
    assert capture_pipeline.is_fetch_stale(capture_pipeline.FETCH_TIMEOUT_SECONDS) is False


def test_失败文案_业务异常用它的中文文案():
    exc = BizException(code=2002, message="网络不给力，换个网络试试")
    assert capture_pipeline.capture_failure_message(exc) == "网络不给力，换个网络试试"


def test_失败文案_未知异常不泄技术细节():
    """技术串进日志，文章上只写面向用户的中文。"""
    msg = capture_pipeline.capture_failure_message(TimeoutError("127.0.0.1:5432 refused"))
    assert "127.0.0.1" not in msg
    assert "5432" not in msg
    assert msg == capture_pipeline.MSG_FETCH_FAILED


# ---------------------------------------------------------------------------
# 2. 接口立即返回（不再等抓取）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_剪藏接口立即返回_不等抓取(fake_fetcher):
    """接口返回时**正文还没抓到**，但文章行已经建好了。

    这是这次改造的核心：客户端不再有「长连接等抓取」这个失败面。
    """
    fake_fetcher.delay = 5.0  # 抓取故意很慢
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://example.com/quick"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["status"] == "pending"
    assert body["fetch_pending"] is True, "客户端要靠它区分「正在抓取」与「待蒸馏」"

    art = await article_row(body["article_id"])
    assert art is not None, "接口返回时文章行必须已经建好"
    assert art.raw_content is None, "正文还没抓到，不该有内容"
    assert art.status == "pending"

    # 抓取还在跑（故意 delay 5s）⇒ 此时**还没扣配额**（先抓后扣）
    assert await quota_used(uid) == 0

    await drain_capture_tasks()


# ---------------------------------------------------------------------------
# 3. 后台抓取成功 ⇒ 写正文 + 扣配额 + 派蒸馏
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_后台抓到正文_扣一次配额并派蒸馏(fake_fetcher, fake_ai_client):
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://example.com/ok"})
    await drain_capture_tasks()

    art = await article_row(r.json()["article_id"])
    assert art is not None
    assert art.raw_content is not None, "抓到正文必须写回文章"
    assert art.status == "pending"  # 抓到正文后进入「等蒸馏」
    assert await quota_used(uid) == 1, "抓到正文才扣配额"
    assert len(fake_ai_client.calls) == 1, "抓到正文后要派蒸馏"
    assert fake_ai_client.calls[0]["article_id"] == art.id


# ---------------------------------------------------------------------------
# 4. 后台抓不到 ⇒ failed + 中文原因 + 不扣配额
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_后台抓不到_标记失败且不扣配额(fake_fetcher, fake_ai_client):
    fake_fetcher.fail = True
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://example.com/fail"})
    await drain_capture_tasks()

    art = await article_row(r.json()["article_id"])
    assert art is not None
    assert art.status == "failed"
    # ⚠️ 必须是**按错误码细分的文案**，不是笼统的「抓取失败，请稍后重试」。
    # 后台抓取失败 → `_prefetch_for_capture` 把 FetcherError 转成带细分文案的
    # BizException → `capture_failure_message` 透传。这条链路要有钉子，
    # 否则未来有人把透传改坏（一律返回通用文案）测试不会红。
    assert (
        art.error == "没能连上这个网站，可能是网络不通或对方站点暂时不可用，请稍后重试"
    ), f"抓取失败应透传细分文案，实际是 {art.error!r}"
    assert art.error != capture_pipeline.MSG_FETCH_FAILED, "不该回落到通用文案"
    assert await quota_used(uid) == 0, "抓不到正文不能扣配额（先抓后扣）"
    assert fake_ai_client.calls == [], "没有正文不许派蒸馏（会拿占位串喂 LLM）"


@pytest.mark.asyncio
async def test_后台抓取失败_按source分流专属文案(monkeypatch):
    """同一错误码，不同 source 的文案不同 —— 后台路径也要分流。

    微信 AUTH 的专属建议是「请在微信里打开后重新复制链接」；对 PDF / 普通网页说
    这句话是胡话（fetchers/__init__.py 实测踩过）。这条钉子证明后台抓取失败时，
    `articles.error` 带的是按 source 细分的文案，而不是笼统的通用文案。
    """
    fetcher = _FakeFetcher(fail=True, error_code=FetcherErrorCode.AUTH, error_source="wechat_mp")
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://mp.weixin.qq.com/s/abc"})
    await drain_capture_tasks()

    art = await article_row(r.json()["article_id"])
    assert art is not None
    assert art.status == "failed"
    assert art.error == (
        "对方网站限制了非官方客户端访问，微信文章请在微信里打开后重新复制链接"
    ), f"微信 AUTH 失败应透传微信专属文案，实际是 {art.error!r}"
    assert art.error != capture_pipeline.MSG_FETCH_FAILED, "不该回落到通用文案"
    assert await quota_used(uid) == 0, "抓不到正文不能扣配额"


@pytest.mark.asyncio
async def test_失败原因能在文章详情里看到(fake_fetcher):
    """失败原因要能被客户端读到（客户端据此显示「为什么失败」）。"""
    fake_fetcher.fail = True
    _uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://example.com/fail-detail"})
        art_id = r.json()["article_id"]
    await drain_capture_tasks()

    async with client(token) as c:
        detail = await c.get(f"/api/v1/articles/{art_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "failed"
    assert body["error"], "详情响应必须带失败原因"
    assert body["fetch_pending"] is False


# ---------------------------------------------------------------------------
# 5. 抓取中重复提交 ⇒ 同一篇、不重复驱动、不重复扣费
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_抓取中重复提交_同一篇且只扣一次(fake_fetcher):
    fake_fetcher.delay = 1.0
    uid, token = await new_user(monthly_quota=5)
    url = "https://example.com/dup"

    async with client(token) as c:
        r1 = await c.post(ARTICLES, json={"url": url})
    await drain_capture_tasks()
    async with client(token) as c:
        r2 = await c.post(ARTICLES, json={"url": url})
    await drain_capture_tasks()

    assert r1.json()["article_id"] == r2.json()["article_id"], "同一链接必须是同一篇"
    assert await quota_used(uid) == 1, "重复提交不能重复扣配额"


# ---------------------------------------------------------------------------
# 6. 抓取失败后再提交 ⇒ 重新驱动抓取
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_失败的文章再提交_重新驱动抓取(fake_fetcher):
    fake_fetcher.fail = True
    uid, token = await new_user(monthly_quota=5)
    url = "https://example.com/retry-fetch"

    async with client(token) as c:
        r1 = await c.post(ARTICLES, json={"url": url})
    await drain_capture_tasks()
    art = await article_row(r1.json()["article_id"])
    assert art is not None and art.status == "failed"

    # 第二次提交：抓取恢复正常 ⇒ 必须**重新驱动**，而不是永远返回那篇失败的
    fake_fetcher.fail = False
    async with client(token) as c:
        r2 = await c.post(ARTICLES, json={"url": url})
    await drain_capture_tasks()

    assert r2.json()["article_id"] == art.id
    art2 = await article_row(art.id)
    assert art2 is not None
    assert art2.status == "pending", "重新抓到正文后要回到 pending（等蒸馏）"
    assert art2.raw_content is not None, "重新抓取必须写回正文"
    assert art2.error is None, "失败原因要清掉"
    assert await quota_used(uid) == 1, "只有成功抓到才计费（失败那次不计费）"


# ---------------------------------------------------------------------------
# 7. 配额用尽 ⇒ 立刻拒，不建文章
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_配额用尽_立刻拒绝且不建文章(fake_fetcher):
    uid, token = await new_user(monthly_quota=0)

    async with client(token) as c:
        r = await c.post(ARTICLES, json={"url": "https://example.com/no-quota"})

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 3001, r.text
    assert await quota_used(uid) == 0

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Article).where(
                        Article.user_id == uid,
                        Article.url == "https://example.com/no-quota",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "被拒的请求不该留下垃圾文章"


# ---------------------------------------------------------------------------
# 8. 兜底定时器：孤儿 pending 收尾
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_兜底定时器_把孤儿标记为失败(fake_fetcher):
    """进程重启 ⇒ 后台任务消失 ⇒ 文章永远 pending。

    兜底定时器必须把它标成 failed（并写明原因），否则用户永远等不到结论。
    """
    uid, token = await new_user(monthly_quota=5)
    art_id = await new_article(uid, status="pending")

    # 把「最后变更时间」改到很久以前（模拟「上一次进程留下的孤儿」）
    #
    # ⚠️ 改的是 `updated_at` 而不是 `created_at`：兜底定时器用 **DB 时钟**
    # 算年龄（`now() - updated_at`）—— created_at 是插入时间，而
    # 「两天前创建、今天重新驱动抓取」的文章不该被当成孤儿。
    async with AsyncSessionLocal() as session:
        art = await session.get(Article, art_id)
        assert art is not None
        art.updated_at = datetime.utcnow() - timedelta(hours=2)
        await session.commit()

    swept = await capture_pipeline.sweep_stale_captures()
    assert swept >= 1, "超时的孤儿没有被收尾"

    art = await article_row(art_id)
    assert art is not None
    assert art.status == "failed"
    assert art.error == capture_pipeline.MSG_FETCH_TIMEOUT


@pytest.mark.asyncio
async def test_兜底定时器_不动正在抓的(fake_fetcher):
    """有在跑标记 ⇒ 不能动它（否则会把正在抓的文章误杀）。"""
    uid, token = await new_user(monthly_quota=5)
    art_id = await new_article(uid, status="pending")

    async with AsyncSessionLocal() as session:
        art = await session.get(Article, art_id)
        assert art is not None
        art.updated_at = datetime.utcnow() - timedelta(hours=2)
        await session.commit()

    await capture_pipeline.mark_inflight(art_id)
    try:
        await capture_pipeline.sweep_stale_captures()
        # 判据是「**这一篇**没被收尾」，不是「本次扫了 0 条」——
        # 开发库里可能还躺着别的历史孤儿，扫到它们是对的。
        art = await article_row(art_id)
        assert art is not None
        assert art.status == "pending", "正在抓的文章被误杀了"
    finally:
        await capture_pipeline.clear_inflight(art_id)


@pytest.mark.asyncio
async def test_兜底定时器_不动已经抓到正文的(fake_fetcher):
    """pending + 有正文 = 在等蒸馏，不是孤儿 ⇒ 不能动。"""
    uid, token = await new_user(monthly_quota=5)
    art_id = await new_article(uid, status="pending", with_content=True)

    async with AsyncSessionLocal() as session:
        art = await session.get(Article, art_id)
        assert art is not None
        art.updated_at = datetime.utcnow() - timedelta(hours=2)
        await session.commit()

    await capture_pipeline.sweep_stale_captures()
    # 判据是「**这一篇**没被收尾」（开发库里可能有别的历史孤儿被顺带扫掉）。
    art = await article_row(art_id)
    assert art is not None
    assert art.status == "pending", "等蒸馏的文章被当成孤儿收尾了"


# ---------------------------------------------------------------------------
# 9. retry 端点：没有正文 ⇒ 重新抓取，不派蒸馏
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_重试_没有正文时重新抓取而不是派蒸馏(fake_fetcher, fake_ai_client):
    uid, token = await new_user(monthly_quota=5)
    art_id = await new_article(uid, status="failed")  # 没有正文 = 抓取失败

    async with client(token) as c:
        r = await c.post(f"/api/v1/articles/{art_id}/retry")
    assert r.status_code == 200, r.text
    assert r.json()["distill_triggered"] is False, "没有正文不该派蒸馏"

    await drain_capture_tasks()
    art = await article_row(art_id)
    assert art is not None
    assert art.raw_content is not None, "重试应该把正文抓回来"
    assert art.status == "pending", "抓回正文后回到 pending（等蒸馏）"
    # 抓到正文**之后**才派蒸馏（一次，且只这篇）
    assert len(fake_ai_client.calls) == 1, "抓到正文后应该派一次蒸馏"
    assert fake_ai_client.calls[0]["article_id"] == art_id


# ---------------------------------------------------------------------------
# 10. 并发闸门（2026-11-11 §9 遗留项）：信号量 + 拒绝快返
# ---------------------------------------------------------------------------
#
# ⚠️ 并发上限 `CAPTURE_MAX_CONCURRENCY` 在**模块 import 期**从 env 读，测试里
# 不能改 env 再 import（模块已被 conftest 加载）。⇒ 用例直接 monkeypatch
# `capture_pipeline.CAPTURE_MAX_CONCURRENCY` 与 `_capture_inflight_count`，
# 把闸门调成「并发 1」来验「满则拒绝」。
#
# 每例都要把计数归零、用例结束把在跑任务 drain 掉 —— 否则计数残留会
# 影响后面的用例（闸门越收越紧）。


@pytest.fixture
def one_slot_gate(monkeypatch):
    """把并发闸门调到 1 个槽，并归零计数（每例独立）。"""
    monkeypatch.setattr(capture_pipeline, "CAPTURE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(capture_pipeline, "_capture_inflight_count", 0)
    yield
    monkeypatch.setattr(capture_pipeline, "_capture_inflight_count", 0)


def test_闸门_占用一个槽后判忙(one_slot_gate):
    """并发满时 `capture_busy` 为真、`acquire_capture_slot` 拒绝（不阻塞）。"""
    assert capture_pipeline.capture_busy() is False
    assert capture_pipeline.acquire_capture_slot() is True
    assert capture_pipeline.capture_busy() is True
    # 满 ⇒ 拒绝（不阻塞）
    assert capture_pipeline.acquire_capture_slot() is False
    # 释放后恢复
    capture_pipeline.release_capture_slot()
    assert capture_pipeline.capture_busy() is False
    assert capture_pipeline.acquire_capture_slot() is True


def test_闸门_释放不会把计数减到负(one_slot_gate):
    """防御：空槽时 release 不把计数减成负（否则闸门永久「未满」）。"""
    capture_pipeline.release_capture_slot()
    assert capture_pipeline.capture_busy() is False


@pytest.mark.asyncio
async def test_并发满_新剪藏被拒绝且标繁忙(fake_fetcher, one_slot_gate):
    """闸门满时，新的剪藏**立即返回失败**（不排队），写明「繁忙」。

    这是「拒绝快返」的核心：不把任务丢进后台排队（那会积压、抬内存），
    而是当场告诉用户「现在抓取忙，稍后再试」。
    """
    fake_fetcher.delay = 5.0  # 第一篇故意抓很久，占住唯一一个槽
    uid, token = await new_user(monthly_quota=5)

    # 第一篇：占住槽位（后台在抓）
    async with client(token) as c:
        r1 = await c.post(ARTICLES, json={"url": "https://example.com/busy-1"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["fetch_pending"] is True

    # 第二篇（不同 URL）：闸满 ⇒ 拒绝快返
    async with client(token) as c:
        r2 = await c.post(ARTICLES, json={"url": "https://example.com/busy-2"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["fetch_pending"] is False, "被拒的文章不是「正在抓」"
    assert body["status"] == "failed", "被拒的文章标成 failed"

    art = await article_row(body["article_id"])
    assert art is not None
    assert art.error == capture_pipeline.MSG_CAPTURE_BUSY, "被拒要写明「繁忙」原因"
    assert await quota_used(uid) == 0, "被拒不扣配额（先抓后扣，抓取没发生）"

    await drain_capture_tasks()


@pytest.mark.asyncio
async def test_并发满_不影响正在抓的重复提交(fake_fetcher, one_slot_gate):
    """同一 URL 正在抓时重复提交 ⇒ 返回同一篇（await），不该被并发闸拒绝。

    闸门只限「新发起的抓取」；已抓的在跑文章重复提交是幂等复用，不占新槽。
    """
    fake_fetcher.delay = 5.0
    uid, token = await new_user(monthly_quota=5)
    url = "https://example.com/busy-dup"

    async with client(token) as c:
        r1 = await c.post(ARTICLES, json={"url": url})
    async with client(token) as c:
        r2 = await c.post(ARTICLES, json={"url": url})

    assert r1.json()["article_id"] == r2.json()["article_id"], "同一链接必须是同一篇"
    assert r2.json()["fetch_pending"] is True, "正在抓的重复提交不该被拒"
    assert r2.json()["status"] == "pending"
    await drain_capture_tasks()
    assert await quota_used(uid) == 1, "只扣一次（不重复计费）"


@pytest.mark.asyncio
async def test_闸_抓取结束槽位释放_后续剪藏可继续(fake_fetcher, one_slot_gate):
    """抓取结束后槽位必须释放：否则闸门越收越紧，最终所有剪藏都被拒。"""
    fake_fetcher.delay = 0.0  # 第一抓立刻结束 → 槽位归还
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        await c.post(ARTICLES, json={"url": "https://example.com/slot-1"})
    await drain_capture_tasks()

    # 槽位已释放 ⇒ 第二篇能正常抓
    async with client(token) as c:
        r2 = await c.post(ARTICLES, json={"url": "https://example.com/slot-2"})
    assert r2.json()["fetch_pending"] is True, "槽位释放后第二篇应被接受"
    await drain_capture_tasks()

    art2 = await article_row(r2.json()["article_id"])
    assert art2 is not None and art2.raw_content is not None, "第二篇应抓到正文"
    assert await quota_used(uid) == 2, "两篇都成功抓到 ⇒ 扣两次"


@pytest.mark.asyncio
async def test_并发满_retry被拒返回繁忙原因(fake_fetcher, one_slot_gate):
    """闸满时点「重试」⇒ 不派抓取，返回繁忙原因，文章标 failed。"""
    fake_fetcher.delay = 5.0
    uid, token = await new_user(monthly_quota=5)

    # 占住槽位
    async with client(token) as c:
        r1 = await c.post(ARTICLES, json={"url": "https://example.com/retry-busy"})
    assert r1.json()["fetch_pending"] is True

    # 造一篇失败文章（无正文），retry 应被并发闸拒
    art_id = await new_article(uid, status="failed")
    async with client(token) as c:
        rr = await c.post(f"/api/v1/articles/{art_id}/retry")
    assert rr.status_code == 200, rr.text
    assert rr.json()["distill_triggered"] is False
    assert rr.json()["status"] == "failed", "被拒后文章应保持 failed"
    assert rr.json()["error"] == capture_pipeline.MSG_CAPTURE_BUSY, "要写明繁忙原因"

    art = await article_row(art_id)
    assert art is not None and art.error == capture_pipeline.MSG_CAPTURE_BUSY
    await drain_capture_tasks()
