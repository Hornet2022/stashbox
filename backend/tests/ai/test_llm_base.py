"""LLMClient abstract base 验证（任务包 §4.1）。"""
import pytest

from llm import LLMClient, MockLLMClient
from llm.types import ChatRequest, ChatResponse

EXPECTED_ABSTRACT = {"chat", "stream", "count_tokens"}


def test_cannot_instantiate_abstract():
    """LLMClient 是 ABC，不能直接实例化。"""
    with pytest.raises(TypeError):
        LLMClient()


def test_abstract_methods_are_the_three():
    assert set(LLMClient.__abstractmethods__) == EXPECTED_ABSTRACT


def test_subclass_missing_methods_cannot_instantiate():
    """少实现任何一个 abstract 方法 → 实例化 TypeError。"""

    class OnlyChat(LLMClient):
        async def chat(self, req: ChatRequest) -> ChatResponse:
            return ChatResponse(content="", model="x")

    with pytest.raises(TypeError):
        OnlyChat()

    class MissingCountTokens(LLMClient):
        async def chat(self, req: ChatRequest) -> ChatResponse:
            return ChatResponse(content="", model="x")

        async def stream(self, req: ChatRequest):
            yield "x"

    with pytest.raises(TypeError):
        MissingCountTokens()


def test_full_subclass_instantiable():
    """三个方法都实现 → 可实例化，且是 LLMClient。"""

    class Full(LLMClient):
        async def chat(self, req: ChatRequest) -> ChatResponse:
            return ChatResponse(content="full", model="x")

        async def stream(self, req: ChatRequest):
            yield "full"

        async def count_tokens(self, text: str, model: str | None = None) -> int:
            return len(text) // 4

    client = Full()
    assert isinstance(client, LLMClient)


async def test_async_context_manager():
    """async with 进出正常，退出时 close() 被调用。"""
    client = MockLLMClient(latency_ms=0)
    async with client as c:
        assert c is client
        resp = await c.chat(ChatRequest(messages=[]))
        assert isinstance(resp, ChatResponse)
    assert client._closed is True


async def test_close_default_is_noop():
    """base 的 close() 默认空实现（没有底层资源的 client 不用重写）。"""

    class NoResource(LLMClient):
        async def chat(self, req: ChatRequest) -> ChatResponse:
            return ChatResponse(content="", model="x")

        async def stream(self, req: ChatRequest):
            yield "x"

        async def count_tokens(self, text: str, model: str | None = None) -> int:
            return 0

    async with NoResource() as c:
        assert await c.count_tokens("abcd") == 0
