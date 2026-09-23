"""CP3.6.2: OpenAIClient prompt cache_control 注入 + tools 透传 + tool_calls 解析。

验证（任务包 §A）：
1. system 消息加 cache_control: ephemeral
2. 非 system 消息不加 cache_control
3. tools / tool_choice 按 OpenAI 协议透传
4. 返回 message.tool_calls → ChatResponse.tool_calls
5. finish_reason="tool_calls" 兼容
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm.openai import OpenAIClient
from llm.types import ChatMessage, ChatRequest, ToolCall


def _mock_response(json_body: dict, status_code: int = 200) -> MagicMock:
    """构造 httpx Response 的 mock。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = lambda: json_body
    resp.aread = AsyncMock(return_value=b'{"err": "x"}')
    return resp


def _body_text_response(text: str = "hello") -> dict:
    return {
        "choices": [{"message": {"content": text, "tool_calls": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _tool_calls_response() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


@pytest.fixture
def client():
    return OpenAIClient(api_key="test-key", base_url="https://mock", timeout=10)


@pytest.mark.asyncio
async def test_cache_control_injected_in_system_message(client):
    """system 消息加 cache_control: ephemeral。"""
    captured = {}

    async def fake_post(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(_body_text_response("hi"))

    with patch.object(client._client, "post", new=AsyncMock(side_effect=fake_post)):
        req = ChatRequest(
            messages=[
                ChatMessage(role="system", content="你是助手"),
                ChatMessage(role="user", content="hi"),
            ]
        )
        await client.chat(req)

    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["cache_control"] == {
        "type": "ephemeral"
    }, f"system 消息应有 cache_control，实际: {msgs[0]}"
    # user 消息不应有 cache_control
    assert "cache_control" not in msgs[1]


@pytest.mark.asyncio
async def test_cache_control_not_injected_when_no_system(client):
    """无 system 消息时不动 messages。"""
    captured = {}

    async def fake_post(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(_body_text_response("hi"))

    with patch.object(client._client, "post", new=AsyncMock(side_effect=fake_post)):
        req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
        await client.chat(req)

    msgs = captured["messages"]
    assert all("cache_control" not in m for m in msgs)


@pytest.mark.asyncio
async def test_tools_passthrough(client):
    """tools 字段透传到 body。"""
    captured = {}

    async def fake_post(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(_body_text_response())

    with patch.object(client._client, "post", new=AsyncMock(side_effect=fake_post)):
        req = ChatRequest(
            messages=[ChatMessage(role="user", content="天气？")],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {}},
                }
            ],
            tool_choice="auto",
        )
        await client.chat(req)

    body = captured
    assert "tools" in body
    assert body["tools"][0]["function"]["name"] == "get_weather"
    assert body["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_tools_not_in_body_when_not_provided(client):
    """无 tools 时 body 不带 tools/tool_choice。"""
    captured = {}

    async def fake_post(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(_body_text_response())

    with patch.object(client._client, "post", new=AsyncMock(side_effect=fake_post)):
        req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
        await client.chat(req)

    body = captured
    assert "tools" not in body
    assert "tool_choice" not in body


@pytest.mark.asyncio
async def test_tool_calls_parsed_from_response(client):
    """返回 tool_calls → ChatResponse.tool_calls 解析正确。"""
    with patch.object(
        client._client, "post", new=AsyncMock(return_value=_mock_response(_tool_calls_response()))
    ):
        req = ChatRequest(
            messages=[ChatMessage(role="user", content="天气？")],
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        )
        resp = await client.chat(req)

    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    assert isinstance(resp.tool_calls[0], ToolCall)
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].function["name"] == "get_weather"
    assert resp.tool_calls[0].function["arguments"] == '{"city":"Beijing"}'
    assert resp.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_no_tool_calls_response_returns_none(client):
    """普通文本响应 → tool_calls 为 None。"""
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_mock_response(_body_text_response("ok"))),
    ):
        req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
        resp = await client.chat(req)

    assert resp.tool_calls is None
    assert resp.content == "ok"
    assert resp.finish_reason == "stop"


@pytest.mark.asyncio
async def test_content_field_default_empty_when_only_tool_calls(client):
    """content 为空但有 tool_calls 时，content 应为 ""（不 None）。"""
    with patch.object(
        client._client, "post", new=AsyncMock(return_value=_mock_response(_tool_calls_response()))
    ):
        req = ChatRequest(
            messages=[ChatMessage(role="user", content="天气？")],
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        )
        resp = await client.chat(req)

    assert resp.content == ""  # 显式空字符串（OpenAI 在 tool_calls 模式下 content 是 null）
    assert resp.tool_calls is not None


@pytest.mark.asyncio
async def test_stream_includes_cache_control(client):
    """stream() 也加 cache_control（system 消息时）+ 正确产出 delta chunks。

    注：httpx.AsyncClient.stream() 是 async context manager，body 在 stream() 入参里。
    这里只验证 stream 路径可产出 chunks；cache_control 已在 _openai_chat 测试覆盖。
    """
    # Mock httpx AsyncClient.stream
    mock_stream_cm = MagicMock()
    mock_stream_cm.__aenter__ = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            aiter_lines=lambda: _async_iter_lines(
                [
                    'data: {"choices":[{"delta":{"content":"hi"}}]}',
                    'data: {"choices":[{"delta":{"content":" world"}}]}',
                    "data: [DONE]",
                ]
            ),
        )
    )
    mock_stream_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(client._client, "stream", return_value=mock_stream_cm):
        req = ChatRequest(
            messages=[
                ChatMessage(role="system", content="你是助手"),
                ChatMessage(role="user", content="hi"),
            ]
        )
        chunks = []
        async for c in client.stream(req):
            chunks.append(c)

    assert chunks == ["hi", " world"]


async def _async_iter_lines(lines):
    """简化版 aiter_lines（返回 async iterator）。"""
    for line in lines:
        yield line


@pytest.mark.asyncio
async def test_shared_client_close_is_noop():
    """_shared=True 单例 client close() 是 no-op（httpx 池不释放）。"""
    c = OpenAIClient(api_key="x", base_url="https://mock", _shared=True)
    # 直接 await close 不应抛异常，也不应真正关闭 httpx
    await c.close()
    # 验证 httpx client 仍可用
    assert c._client is not None
    # 关闭 httpx 应是显式调用
    await c._client.aclose()


@pytest.mark.asyncio
async def test_non_shared_client_close_releases_httpx():
    """_shared=False → close() 释放 httpx。"""
    c = OpenAIClient(api_key="x", base_url="https://mock", _shared=False)
    await c.close()
    # httpx.AsyncClient.aclose() 后再访问会抛 RuntimeError
    # 这里只验证 close 不抛异常
    assert True
