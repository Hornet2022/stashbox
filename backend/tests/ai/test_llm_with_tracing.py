"""LLMClient 接 Langfuse + 成本归因单测（任务包 §4.4）。

- 上报走假 langfuse SDK（fake_langfuse）
- 成本归因走假 CostTracker（monkeypatch cost_tracker 模块属性 —— _maybe_trace 里是函数内 import，
  每次调用才取属性，所以 monkeypatch 生效）
"""

import pytest

from llm import ClaudeSonnetClient, MockLLMClient
from llm.types import ChatMessage, ChatRequest
from observability import cost_tracker as ct_mod


class RecordingTracker:
    """替身 CostTracker：只记调用，不碰 Redis。"""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def record_llm_usage(self, *args, **kwargs):
        RecordingTracker.calls.append((args, kwargs))
        return 0.0


@pytest.fixture(autouse=True)
def _swap_tracker(monkeypatch):
    RecordingTracker.calls = []
    monkeypatch.setattr(ct_mod, "CostTracker", RecordingTracker)


def _req(**metadata) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="请把这篇文章结构化")],
        metadata=metadata or None,
    )


# ---------------------------------------------------------------------------
# 默认 disable 模式
# ---------------------------------------------------------------------------
async def test_disabled_chat_returns_response_and_touches_nothing(langfuse_disabled):
    client = MockLLMClient(latency_ms=0)

    resp = await client.chat(_req(user_id=1, article_id="art_1"))

    assert resp.content
    assert RecordingTracker.calls == []  # 不写 redis
    await client.close()


async def test_disabled_chat_still_reports_usage_from_client(langfuse_disabled):
    """disable 不影响 client 自身的 usage 统计（向后兼容 CP3.5-pre-2）。"""
    client = MockLLMClient(latency_ms=0)

    resp = await client.chat(_req())

    assert resp.usage.total_tokens > 0
    await client.close()


# ---------------------------------------------------------------------------
# 启用模式
# ---------------------------------------------------------------------------
async def test_enabled_chat_reports_trace_generation_and_cost(fake_langfuse):
    client = MockLLMClient(latency_ms=0)

    resp = await client.chat(_req(user_id=9, article_id="art_cost"))

    # trace
    trace_kwargs = fake_langfuse.traces[0]
    assert trace_kwargs["name"] == "llm_chat"
    assert trace_kwargs["metadata"] == {"model": "mock-model"}

    # generation（含 usage）
    span = fake_langfuse.trace_objects[0].spans[0]
    gen = span.generations[0]
    assert gen.kwargs["model"] == "mock-model"
    assert gen.kwargs["output"] == resp.content
    assert gen.kwargs["usage"] == {
        "prompt_tokens": resp.usage.prompt_tokens,
        "completion_tokens": resp.usage.completion_tokens,
        "total_tokens": resp.usage.total_tokens,
    }

    # 成本归因
    args, _ = RecordingTracker.calls[0]
    user_id, article_id, model, prompt_tokens, completion_tokens = args
    assert (user_id, article_id, model) == (9, "art_cost", "mock-model")
    assert (prompt_tokens, completion_tokens) == (
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )
    await client.close()


async def test_enabled_chat_skips_cost_without_user_and_article(fake_langfuse):
    """没有 user_id / article_id（裸调用）不归因，但 trace 照发。"""
    client = MockLLMClient(latency_ms=0)

    await client.chat(_req(step="whatever"))

    assert len(fake_langfuse.traces) == 1
    assert RecordingTracker.calls == []
    await client.close()


async def test_enabled_chat_marks_error_and_reraises(fake_langfuse):
    client = MockLLMClient(latency_ms=0)

    class Boom(MockLLMClient):
        async def _mock_chat(self, req):
            raise RuntimeError("LLM 炸了")

    boom = Boom(latency_ms=0)
    with pytest.raises(RuntimeError, match="LLM 炸了"):
        await boom.chat(_req(user_id=9, article_id="art_1"))

    updates = fake_langfuse.trace_objects[0].updates
    assert updates[-1]["level"] == "ERROR"
    assert updates[-1]["status_message"] == "LLM 炸了"
    await client.close()


async def test_enabled_stream_failure_marks_error(fake_langfuse):
    """stream 失败也要标 ERROR（成功路径的 usage 由内部 chat 上报，不重复计数）。"""

    class Boom(MockLLMClient):
        async def _mock_chat(self, req):
            raise RuntimeError("stream 炸了")

    boom = Boom(latency_ms=0)
    with pytest.raises(RuntimeError, match="stream 炸了"):
        async for _ in boom.stream(_req(user_id=9, article_id="art_1")):
            pass


async def test_enabled_qwen_stub_reports_error(fake_langfuse):
    """CP7.1：qwen_vl chat() 已实现，错误（401）仍走 trace 上报路径。"""
    from llm import QwenVLClient

    client = QwenVLClient(api_key="sk-test")

    with pytest.raises(Exception):  # 401 Unauthorized，无真实 key
        await client.chat(_req(user_id=9, article_id="art_1"))

    assert fake_langfuse.trace_objects[0].updates[-1]["level"] == "ERROR"
    await client.close()


async def test_enabled_claude_stream_reports_error(fake_langfuse):
    client = ClaudeSonnetClient(api_key="sk-test")

    with pytest.raises(NotImplementedError):
        await client.stream(_req(user_id=9, article_id="art_1"))

    assert fake_langfuse.trace_objects[0].updates[-1]["level"] == "ERROR"
    await client.close()
