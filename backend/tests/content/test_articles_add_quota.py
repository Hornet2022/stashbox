"""旧版剪藏入口 `POST /api/v1/articles/add` 的**配额口径**回归。

## 为什么要有这个文件

2026-10-25 之前，`POST /api/v1/articles/add`（deprecated，给老客户端留的）
**整段绕过了剪藏公共后半程** —— 它自己调 `_create_article`，于是：

  · **不扣配额**；
  · 不打 `ARTICLE_SUBMIT` 埋点；
  · 不做 `cache_service.mark_article_quota` 标记。

后果不是「老接口宽松一点」：

  · 每次调用都派一次**真实**蒸馏（LLM + IndexTTS，单篇约 23 分钟 GPU）
    而配额一次都不扣 ⇒ 任何持 token 的用户可以无限免费剪藏不同 URL；
  · 剪藏路径**没有任何速率限制**（网关的 `_CAPTURE_PATHS` 只影响超时文案）；
  · 配额是运营侧唯一的用量口径 ⇒ 这条链上的消耗在账上看不见。

而同一族端点（`/api/v1/articles`、`/api/v1/articles/document`、
`/api/v1/admin/articles`、`/api/v1/callback/d9-add-article`）**都扣**。

## 覆盖到的行为

· 旧端点剪藏 → 扣 1 次配额 + 建库 + 派蒸馏 + 打 ARTICLE_SUBMIT 埋点；
· 配额用尽 → 403 / 业务码 3001（与新端点同口径）；
· 同一 URL 重复剪藏 → 复用同一篇，**不重复扣费**；
· 响应头仍带 `Deprecation` / `Sunset` / `Link`（弃用契约不能因为改了逻辑就丢）；
· 与新端点 `POST /api/v1/articles` 扣的是**同一个**配额池。

## 抓取层 mock

沿用 D9 用例的做法：把 `get_fetcher` 整体换掉，一个外网请求都不发
（否则单文件会跑到 >45s，离线环境直接挂）。
"""

from __future__ import annotations

import pytest

from fetchers.base import FetchResult

from helpers import (
    article_row,
    client,
    content_main,
    feedback_count,
    new_user,
    quota_used,
)

ARTICLES_ADD = "/api/v1/articles/add"
ARTICLES = "/api/v1/articles"


# ---------------------------------------------------------------------------
# 抓取层 mock：切断一切外网请求
# ---------------------------------------------------------------------------
class _FakeFetcher:
    """最小 fetcher 替身：返回一个够长的正文（正文长度有下限）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
        self.calls.append(url)
        return FetchResult(
            url=url,
            title="测试文章",
            content_html="<p>正文</p>",
            content_text="这是测试用的正文内容。" * 40,
            source="wechat_mp",
        )


@pytest.fixture(autouse=True)
def fake_fetcher(monkeypatch) -> _FakeFetcher:
    fetcher = _FakeFetcher()
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    return fetcher


# ---------------------------------------------------------------------------
# 1. 扣配额（缺陷本身）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点剪藏要扣配额():
    """这是缺陷本身的回归：修前 quota_used 恒为 0。

    断言的是**库的配额计数**，不是响应体 —— 响应体里没有配额字段时，
    「扣了没有」只有库知道。
    """
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES_ADD, json={"url": "https://example.com/quota-1"})

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"].startswith("art_"), "旧端点仍返回文章本体（ArticleResponse）"

    assert await quota_used(uid) == 1, "旧端点剪藏必须扣 1 次配额"

    art = await article_row(data["id"])
    assert art is not None
    assert art.user_id == uid


# ---------------------------------------------------------------------------
# 2. 与新端点扣的是同一个配额池
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点与新端点扣同一个配额池():
    """两个端点各剪藏一次（不同 URL）→ 累计扣 2 次。

    口径不一致的另一种表现是「各扣各的」：这里断言的是**同一个 user 的
    同一个计数**，所以任何一边不扣或各扣各的都会被这条抓住。
    """
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r_old = await c.post(ARTICLES_ADD, json={"url": "https://example.com/pool-old"})
        r_new = await c.post(ARTICLES, json={"url": "https://example.com/pool-new"})

    assert r_old.status_code == 200, r_old.text
    assert r_new.status_code == 200, r_new.text
    assert await quota_used(uid) == 2, "两个端点应累计扣 2 次"


# ---------------------------------------------------------------------------
# 3. 配额用尽 → 3001（与新端点同口径）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点配额用尽报3001():
    """monthly_quota=0 → 第一次剪藏就被拦下。

    修前这条端点**根本不看配额**，所以能无限建文章 —— 这是成本暴露的根源。
    """
    uid, token = await new_user(monthly_quota=0)

    async with client(token) as c:
        r = await c.post(ARTICLES_ADD, json={"url": "https://example.com/no-quota"})

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 3001, r.text
    assert await quota_used(uid) == 0, "被拦下的请求不该扣配额"


# ---------------------------------------------------------------------------
# 4. 重复剪藏 → 复用且不重复扣费
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点重复剪藏同一链接不重复扣费():
    uid, token = await new_user(monthly_quota=5)
    url = "https://example.com/same-url"

    async with client(token) as c:
        r1 = await c.post(ARTICLES_ADD, json={"url": url})
        r2 = await c.post(ARTICLES_ADD, json={"url": url})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] == r2.json()["id"], "同一链接应复用同一篇文章"
    assert await quota_used(uid) == 1, "复用不该再扣配额"


# ---------------------------------------------------------------------------
# 5. 弃用契约头不能因为改了逻辑就丢
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点仍带弃用响应头():
    """`Deprecation` / `Sunset` / `Link` 是「告诉客户端去迁移」这句话本身。

    丢了它，弃用机制在生产完全失效（老客户端永远不迁）。
    另注：这三个头能不能真的到客户端，还取决于**网关透传**——
    那半截在 tests/gateway/test_response_header_passthrough.py 里。
    """
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.post(ARTICLES_ADD, json={"url": "https://example.com/deprecation"})

    assert r.status_code == 200, r.text
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "CP12"
    assert "successor-version" in (r.headers.get("Link") or "")


# ---------------------------------------------------------------------------
# 6. 埋点：ARTICLE_SUBMIT 不能少
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_旧端点打ARTICLE_SUBMIT埋点():
    """旧端点以前只建文章、不埋点 ⇒ 运营侧的「剪藏量」统计漏掉这一整条链。"""
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(ARTICLES_ADD, json={"url": "https://example.com/event"})

    assert r.status_code == 200, r.text
    art_id = r.json()["id"]
    assert await feedback_count(uid, art_id, "article_submit") == 1
