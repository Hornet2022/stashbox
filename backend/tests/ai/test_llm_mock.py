"""MockLLMClient 测试（任务包 §4.2）。"""
import asyncio
import inspect
import json
import time

from llm import MockLLMClient
from llm.types import ChatRequest, ChatMessage, ChatResponse


def _req(text: str) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content=text)])


async def test_chat_returns_chat_response_with_usage_and_latency():
    client = MockLLMClient(latency_ms=100.0)
    resp = await client.chat(_req("帮我看看这篇文章"))

    assert isinstance(resp, ChatResponse)
    assert resp.content
    assert resp.model == "mock-model"
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens
    assert resp.usage.prompt_tokens > 0
    assert resp.finish_reason == "stop"
    assert resp.latency_ms >= 90  # sleep(100ms)
    await client.close()


async def test_chat_routes_structured_prompt_to_json():
    """v1 §5.2.2 Step 1（Qwen2.5-VL 内容结构化）风格：关键词「结构化」→ JSON。"""
    client = MockLLMClient(latency_ms=0)
    resp = await client.chat(_req("请把这篇文章内容结构化"))

    payload = json.loads(resp.content)
    assert payload["summary"]
    assert payload["chapters"]
    assert payload["entities"]
    await client.close()


async def test_chat_routes_rewrite_prompt_to_colloquial():
    """v1 §5.2.3 Step 2（Claude 听感改写）风格：关键词「改写」→ 口语化稿。"""
    client = MockLLMClient(latency_ms=0)
    resp = await client.chat(_req("请把下面的内容改写成播客口播稿"))

    assert "嘿" in resp.content
    assert not resp.content.startswith("{")
    await client.close()


async def test_chat_falls_back_to_default_response():
    client = MockLLMClient(latency_ms=0)
    resp = await client.chat(_req("今天天气怎么样"))

    assert "llm_provider" in resp.content  # 提示如何切真 API
    await client.close()


async def test_stream_yields_chunks():
    client = MockLLMClient(latency_ms=0)
    req = _req("今天天气怎么样")

    assert inspect.isasyncgen(client.stream(req))  # 真 async generator，不是阻塞迭代器

    chunks = [chunk async for chunk in client.stream(req)]
    assert len(chunks) >= 2
    assert "".join(chunks[:-1]) == (await client.chat(req)).content  # 末块是尾部空串 + "。"
    await client.close()


async def test_stream_is_not_blocking():
    """两个流并发消费能交错（串行要 ~0.30s，并发 ~0.15s）。"""
    client = MockLLMClient(latency_ms=0)

    async def consume() -> list[str]:
        return [chunk async for chunk in client.stream(_req("今天天气怎么样"))]

    start = time.perf_counter()
    first, second = await asyncio.gather(consume(), consume())
    elapsed = time.perf_counter() - start

    assert first == second
    assert elapsed < 0.25
    await client.close()


async def test_count_tokens_is_quarter_of_length():
    client = MockLLMClient()
    assert await client.count_tokens("a" * 100) == 25
    assert await client.count_tokens("abcd", model="whatever") == 1
    await client.close()


async def test_close_marks_closed():
    client = MockLLMClient()
    assert client._closed is False
    await client.close()
    assert client._closed is True
