"""QwenVLClient 接口签名测试（任务包 §4.5 —— 本期不接 DashScope，只验证签名 + 鉴权）。"""

import pytest

from llm.qwen_vl import QWEN_VL_API_URL, QWEN_VL_DEFAULT_MODEL, QwenVLClient
from llm.types import ChatMessage, ChatRequest

API_KEY = "sk-dashscope-test"


def _req(**kwargs) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="结构化这篇文章")],
        **kwargs,
    )


@pytest.fixture
async def client():
    c = QwenVLClient(api_key=API_KEY)
    yield c
    await c.close()


async def test_constructor_requires_api_key():
    with pytest.raises(ValueError, match="api_key required"):
        QwenVLClient(api_key="")


async def test_bearer_token_header(client):
    assert client._client.headers["authorization"] == f"Bearer {API_KEY}"
    assert client._client.headers["content-type"] == "application/json"


async def test_defaults_point_to_dashscope(client):
    assert client.model == QWEN_VL_DEFAULT_MODEL
    assert client.base_url == QWEN_VL_API_URL


async def test_chat_not_implemented(client):
    """CP7.1：chat() 已实现，走 OpenAI 兼容端点。"""
    # 真实 API 未配置 key 时会 401，这里只验证 URL 结构正确
    with pytest.raises(Exception, match="401"):
        await client.chat(_req(model=QWEN_VL_DEFAULT_MODEL))


async def test_stream_not_implemented(client):
    """CP7.1：stream() 已实现，走 OpenAI 兼容端点（异步生成器，需 async for 消费）。"""
    # 真实 API 未配置 key 时会 401，async for 触发实际请求
    with pytest.raises(Exception, match="401"):
        async for _ in client.stream(_req()):
            pass


async def test_count_tokens_is_quarter_of_length(client):
    assert await client.count_tokens("a" * 100) == 25


async def test_close_releases_httpx_session():
    client = QwenVLClient(api_key=API_KEY)
    assert client._client.is_closed is False
    await client.close()
    assert client._client.is_closed is True
