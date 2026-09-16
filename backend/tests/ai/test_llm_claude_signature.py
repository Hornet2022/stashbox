"""ClaudeSonnetClient 接口签名测试（任务包 §4.4 —— httpx 走 MockTransport，不发真请求）。"""
import asyncio
import json

import httpx
import pytest

from llm.claude import CLAUDE_API_URL, CLAUDE_DEFAULT_MODEL, ClaudeSonnetClient
from llm.exceptions import LLMError, RateLimitError
from llm.types import ChatMessage, ChatRequest

API_KEY = "sk-ant-test"

OK_BODY = {
    "content": [{"type": "text", "text": "嘿！今天咱们聊聊 AI。"}],
    "usage": {"input_tokens": 30, "output_tokens": 12},
    "stop_reason": "end_turn",
}


def _req(**kwargs) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="你是播客主播"),
            ChatMessage(role="user", content="改写这段内容"),
        ],
        **kwargs,
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """retry 的指数退避别真睡（否则单测要等好几秒）。"""

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


@pytest.fixture
async def client():
    """真 ClaudeSonnetClient（httpx session 已建），但 transport 由每个 case 自己换。"""
    c = ClaudeSonnetClient(api_key=API_KEY, max_retries=2)
    await c._client.aclose()  # 卸掉真实 session，杜绝真请求
    yield c
    await c.close()


def _mount(client: ClaudeSonnetClient, handler) -> None:
    """换上 MockTransport（沿用 __init__ 造的鉴权 header），请求不出本机。"""
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=dict(client._client.headers)
    )


async def test_constructor_requires_api_key():
    with pytest.raises(ValueError, match="api_key required"):
        ClaudeSonnetClient(api_key="")


async def test_httpx_client_created_with_anthropic_headers():
    client = ClaudeSonnetClient(api_key=API_KEY)
    try:
        assert client._client.headers["x-api-key"] == API_KEY
        assert client._client.headers["anthropic-version"] == "2023-06-01"
        assert client.model == CLAUDE_DEFAULT_MODEL
        assert client.base_url == CLAUDE_API_URL
    finally:
        await client.close()


async def test_chat_parses_response(client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    _mount(client, handler)
    resp = await client.chat(_req())

    assert resp.content == "嘿！今天咱们聊聊 AI。"
    assert resp.model == CLAUDE_DEFAULT_MODEL
    assert resp.usage.prompt_tokens == 30
    assert resp.usage.completion_tokens == 12
    assert resp.usage.total_tokens == 42
    assert resp.finish_reason == "stop"
    assert resp.latency_ms >= 0
    assert len(seen) == 1


async def test_chat_request_body_follows_anthropic_format(client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    _mount(client, handler)
    await client.chat(_req(stop=["END"], model="claude-4-sonnet-20250514"))

    request = seen[0]
    assert str(request.url) == CLAUDE_API_URL
    assert request.headers["x-api-key"] == API_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"

    body = json.loads(request.content)
    assert body["system"] == "你是播客主播"  # system 单独字段
    assert body["messages"] == [{"role": "user", "content": "改写这段内容"}]  # 不含 system
    assert body["model"] == "claude-4-sonnet-20250514"
    assert body["max_tokens"] == 4096
    assert body["stop_sequences"] == ["END"]


async def test_chat_stop_reason_max_tokens_maps_to_length(client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={**OK_BODY, "stop_reason": "max_tokens"},
        )

    _mount(client, handler)
    resp = await client.chat(_req())
    assert resp.finish_reason == "length"


async def test_chat_raises_rate_limit_error_on_429(client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate limited"})

    _mount(client, handler)
    with pytest.raises(RateLimitError) as exc:
        await client.chat(_req())
    assert exc.value.retry_after == "30"


async def test_chat_retries_then_raises_llm_error(client, no_sleep):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "internal"})

    _mount(client, handler)
    with pytest.raises(LLMError, match="claude chat failed"):
        await client.chat(_req())
    assert len(calls) == client.max_retries  # 重试到上限


async def test_chat_retries_then_succeeds(client, no_sleep):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(200, json=OK_BODY)

    _mount(client, handler)
    resp = await client.chat(_req())
    assert len(calls) == 2
    assert resp.content == OK_BODY["content"][0]["text"]


async def test_stream_not_implemented(client):
    """真 SSE 流式留 CP3.5。"""
    with pytest.raises(NotImplementedError):
        await client.stream(_req())


async def test_count_tokens_is_quarter_of_length(client):
    assert await client.count_tokens("a" * 100) == 25
    assert await client.count_tokens("abcd", model="claude-4-sonnet-20250514") == 1


async def test_close_releases_httpx_session():
    client = ClaudeSonnetClient(api_key=API_KEY)
    assert client._client.is_closed is False
    await client.close()
    assert client._client.is_closed is True
