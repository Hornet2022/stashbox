"""ClawBot 回调入口：POST /api/v1/callback/clawbot-message。

覆盖（2026-10-22 走查后补）：这个端点原来只有三行，且有三个洞：

1. **不扣配额** → 登录用户可以无限建文章（换 URL 就能一直建，
   articles 表对 (user_id, url) 没有唯一约束）→ 配额纹丝不动，
   列表里堆满永远 pending 的条目。
2. **不派蒸馏** → 建出来的文章永远是 pending，用户点播放没反应。
3. **把整段 text 当 URL** → `"http" in req.text` 只判断有没有 http，
   传「看看这个 https://xxx」进去，url 存的是**整句话** → 抓取必然失败。

现在与 `submit_article` 同款：抽 URL → 校验 → 先抓后扣 → 扣配额 →
建文章 → 打「已扣费」标记 → 派蒸馏。

**配额是这条路径唯一的天然上限** —— 洞就是被它堵上的。
"""

import uuid

import pytest

from fetchers.base import FetchResult

from helpers import client, content_main, new_user, quota_used

CLAWBOT_URL = "/api/v1/callback/clawbot-message"


class _FakeFetcher:
    """最小 fetcher 替身 —— 切断一切外网请求（否则会真去连 mp.weixin.qq.com）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
        self.calls.append(url)
        return FetchResult(
            url=url,
            title="测试文章标题",
            content_html="<p>正文</p>",
            content_text="测试正文内容",
            source="wechat_mp",
        )


@pytest.fixture(autouse=True)
def fake_fetcher(monkeypatch) -> _FakeFetcher:
    fetcher = _FakeFetcher()
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    return fetcher


async def test_文本里带链接_要抽出URL而不是整句话(fake_ai_client, fake_fetcher):
    """洞 3：原文里混着话时，url 必须是**抽出来的那个链接**。"""
    uid, token = await new_user()
    text = f"看看这个 https://mp.weixin.qq.com/s/claw_{uuid.uuid4().hex[:8]}"
    try:
        async with client(token) as c:
            r = await c.post(CLAWBOT_URL, json={"text": text})
        assert r.status_code == 200, r.text
        art = r.json()["article"]
        assert art["url"] == "https://" + text.split("https://", 1)[1]
        assert art["url"].startswith("https://mp.weixin.qq.com/s/claw_")
        # 抓取器拿到的也必须是抽出来的 URL
        assert fake_fetcher.calls == [art["url"]]
    finally:
        await _purge(uid)


async def test_建文章要扣配额(fake_ai_client):
    """洞 1：不扣配额 = 登录用户能无限建文章。"""
    uid, token = await new_user()
    before = await quota_used(uid)
    url = f"https://mp.weixin.qq.com/s/clawq_{uuid.uuid4().hex[:8]}"
    try:
        async with client(token) as c:
            r = await c.post(CLAWBOT_URL, json={"text": url})
        assert r.status_code == 200, r.text
        after = await quota_used(uid)
        assert after == before + 1, f"建文章必须扣一次配额：{before} → {after}"
        assert r.json()["remaining"] >= 0
    finally:
        await _purge(uid)


async def test_建文章要派蒸馏(fake_ai_client):
    """洞 2：不派蒸馏 = 文章永远 pending，用户点播放没反应。"""
    uid, token = await new_user()
    url = f"https://mp.weixin.qq.com/s/clawd_{uuid.uuid4().hex[:8]}"
    try:
        async with client(token) as c:
            r = await c.post(CLAWBOT_URL, json={"text": url})
        assert r.status_code == 200, r.text
        art_id = r.json()["article"]["id"]
        assert any(
            call["article_id"] == art_id for call in fake_ai_client.calls
        ), "建完文章必须派蒸馏，否则它永远是 pending"
    finally:
        await _purge(uid)


async def test_没有链接_照收但不建文章也不扣配额(fake_ai_client):
    """一句闲聊不该报错，也不该建文章、更不该扣钱。"""
    uid, token = await new_user()
    before = await quota_used(uid)
    try:
        async with client(token) as c:
            r = await c.post(CLAWBOT_URL, json={"text": "今天天气不错"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["received"] is True
        assert body["article"] is None
        assert await quota_used(uid) == before, "没有链接就不该扣配额"
    finally:
        await _purge(uid)


async def test_未登录_401(fake_ai_client):
    async with client() as c:
        r = await c.post(CLAWBOT_URL, json={"text": "https://mp.weixin.qq.com/s/x"})
    assert r.status_code == 401, r.text


async def _purge(user_id: int) -> None:
    from sqlalchemy import delete

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import Article

    async with AsyncSessionLocal() as s:
        await s.execute(delete(Article).where(Article.user_id == user_id))
        await s.commit()
