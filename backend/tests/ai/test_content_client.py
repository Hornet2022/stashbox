"""ai-service → content-service 的补图注客户端（2026-10-25）。

## 为什么这个客户端**必须允许失败**

识图失败只意味着「这篇没有图表信息」，正文照常能蒸馏。
⇒ 这里的任何失败都**降级成 0**，不抛异常 ——
否则就是「为了可有可无的图表，把整篇文章的稿子也赔进去」。

## 这些用例锁的是三件事

1. **请求对** —— URL 对（`content_service_url` + 内部端点路径）、密钥头对
   （没带密钥的话 content-service 会 503，等于这个功能从来没生效过）。
2. **失败降级** —— 非 200、响应不是 JSON、网络异常，统统返回 0 且不抛。
3. **没配密钥就不调** —— fail-closed：不发这个请求（省一次无谓的网络往返，
   也避免把"没配"这件事藏在一个 503 里）。

⚠️ 全部不打真网络：httpx.MockTransport 注入。
"""

from __future__ import annotations

import httpx
import pytest

from services import content_client

SECRET = "test-caption-secret"
ARTICLE_ID = "art_test_123"


@pytest.fixture
def configured_secret(monkeypatch):
    monkeypatch.setattr(content_client.settings, "callback_shared_secret", SECRET, raising=False)
    return SECRET


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. 请求对
# ---------------------------------------------------------------------------
async def test_带密钥_打到正确的内部端点(configured_secret, monkeypatch):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, json={"captions": 3, "images": 5, "changed": True})

    monkeypatch.setattr(
        content_client.settings, "content_service_url", "http://content-service:8002"
    )

    count = await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler))

    assert count == 3
    req = seen["req"]
    assert str(req.url) == (
        f"http://content-service:8002/api/v1/internal/articles/{ARTICLE_ID}/captions"
    )
    assert req.headers["X-Callback-Secret"] == SECRET, "没带密钥的话端点会 503"


async def test_url_末尾斜杠不会拼出双斜杠(configured_secret, monkeypatch):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, json={"captions": 1})

    monkeypatch.setattr(content_client.settings, "content_service_url", "http://127.0.0.1:8102/")
    await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler))

    assert "//api" not in str(seen["req"].url)
    assert str(seen["req"].url).startswith("http://127.0.0.1:8102/api/")


# ---------------------------------------------------------------------------
# 2. 失败降级（统统返回 0，不抛）
# ---------------------------------------------------------------------------
async def test_非_200_降级成_0(configured_secret):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "没配密钥"})

    assert (
        await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler)) == 0
    )


async def test_响应不是_json_降级成_0(configured_secret):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert (
        await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler)) == 0
    )


async def test_网络异常_降级成_0(configured_secret):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上", request=request)

    assert (
        await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler)) == 0
    )


# ---------------------------------------------------------------------------
# 3. 没配密钥就不调（fail-closed）
# ---------------------------------------------------------------------------
async def test_没配密钥_不发请求(monkeypatch):
    monkeypatch.setattr(content_client.settings, "callback_shared_secret", "", raising=False)

    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"captions": 9})

    assert (
        await content_client.fill_article_captions(ARTICLE_ID, transport=_transport(handler)) == 0
    )
    assert called["n"] == 0, "没配密钥就不该发这个请求"
