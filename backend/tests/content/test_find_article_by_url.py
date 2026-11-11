"""按 URL 幂等回查（`GET /api/v1/articles/by-url`）—— 2026-11-11。

## 为什么要有这个端点

剪藏是**同步长请求**（服务端当场抓取，几秒到几十秒才返回）。
客户端在等响应时任何客户端侧抖动（切网 / NAT 回收 / HTTP2 流重置）
都会变成「**服务端成功、客户端报错**」。

实测（uid 23，2026-11-11 17:40~17:43）：4 次剪藏服务端**全部 200**、
文章**全部建好并进入蒸馏**，客户端却弹了「网络不给力」——
用户以为 4 次都失败了，重启 App 才发现文章都在。

⇒ 客户端失败时回查一次：文章在 ⇒ 告诉用户「已提交，正在蒸馏」。

## 这组用例锁住三件事

· **口径与剪藏时的早查重一致**（同用户 + 同 URL + 未删除）——
  两处口径漂了就会出现"剪藏时算重复、回查时算没有"；
· **归属**：别人的文章查不到（越权一律当"没有"）；
· **路由顺序**：`by-url` 必须注册在 `{article_id}` **之前**，
  否则 "by-url" 会被当成 article_id（FastAPI 按声明顺序匹配）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from helpers import client, new_user
from sqlalchemy import delete
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article

URL = "/api/v1/articles/by-url"


async def _article_with_url(user_id: int, url: str) -> str:
    """建一条**指定 URL** 的文章（helpers.new_article 的 URL 是写死的）。"""
    async with AsyncSessionLocal() as session:
        art = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url=url,
            source="d9",
            status="pending",
            favorite=False,
            skip=False,
        )
        session.add(art)
        await session.commit()
        return art.id


async def _purge(*user_ids: int) -> None:
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


async def test_查不到时返回exists_false():
    uid, token = await new_user()
    url = f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex[:12]}"
    try:
        async with client(token) as c:
            r = await c.get(URL, params={"url": url})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["exists"] is False
        assert body["article_id"] is None
    finally:
        await _purge(uid)


async def test_剪藏过就能查到():
    uid, token = await new_user()
    url = f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex[:12]}"
    art_id = await _article_with_url(uid, url)
    try:
        async with client(token) as c:
            r = await c.get(URL, params={"url": url})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["exists"] is True
        assert body["article_id"] == art_id
        assert body["status"] == "pending"
    finally:
        await _purge(uid)


async def test_别人的文章查不到():
    """归属边界：越权一律当"没有"（不泄露存在性）。"""
    owner, _ = await new_user()
    other, other_token = await new_user()
    url = f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex[:12]}"
    await _article_with_url(owner, url)
    try:
        async with client(other_token) as c:
            r = await c.get(URL, params={"url": url})
        assert r.status_code == 200, r.text
        assert r.json()["exists"] is False
    finally:
        await _purge(owner, other)


async def test_未登录不能查():
    """鉴权：没有 token ⇒ 401（不能匿名探测别人剪藏过什么）。"""
    async with client() as c:
        r = await c.get(URL, params={"url": "https://example.com/x"})
    assert r.status_code in (401, 403), r.text


async def test_网关路由表必须显式注册这个端点():
    """显式注册（而不是靠前缀兜底）—— 兜底路径不注入 X-Request-ID。

    网关日志与下游日志串不上号 ⇒「剪藏失败要回溯扣费与蒸馏派发」时
    根本对不上是哪一次请求 ⇒ 排查成本翻倍。
    （路由覆盖测试**故意**允许前缀兜底，所以这里单独立一条更严的守卫。）
    """
    import sys

    gateway_dir = Path(__file__).resolve().parents[2] / "api-gateway"
    if str(gateway_dir) not in sys.path:
        sys.path.insert(0, str(gateway_dir))
    from config import ROUTES  # noqa: PLC0415

    registered = [(r.method, r.path) for r in ROUTES]
    assert ("GET", "/api/v1/articles/by-url") in registered


async def test_按声明顺序by_url不会被当成article_id():
    """路由顺序：`by-url` 必须排在 `{article_id}` 之前。

    否则 FastAPI 会把 "by-url" 当成 article_id ⇒ 查文章详情 ⇒ 404，
    客户端回查永远查不到 ⇒ 修复形同虚设。
    """
    from helpers import content_main

    paths = [getattr(r, "path", None) for r in content_main.app.routes]
    assert "/api/v1/articles/by-url" in paths
    assert paths.index("/api/v1/articles/by-url") < paths.index("/api/v1/articles/{article_id}")
