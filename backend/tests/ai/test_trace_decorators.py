"""@trace_llm_call / @trace_distill_step 装饰器单测（任务包 §4.2）。"""
import pytest

from llm.types import ChatMessage, ChatRequest, ChatResponse, Usage
from observability.decorators import trace_distill_step, trace_llm_call


class TracedLLM:
    """最小的 LLMClient 形状，用来验装饰器。"""

    def __init__(self, *, raise_error: Exception | None = None):
        self.raise_error = raise_error

    @trace_llm_call("unit_chat")
    async def chat(self, req: ChatRequest) -> ChatResponse:
        if self.raise_error is not None:
            raise self.raise_error
        self.last_req = req
        return ChatResponse(
            content="改写稿",
            model="claude-4-sonnet-20250514",
            usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )


def _req(**metadata) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="请把这篇改写一下")],
        model="claude-4-sonnet-20250514",
        metadata=metadata or None,
    )


# ---------------------------------------------------------------------------
# @trace_llm_call
# ---------------------------------------------------------------------------
async def test_trace_llm_call_disabled_just_runs_the_function(langfuse_disabled):
    llm = TracedLLM()

    resp = await llm.chat(_req(user_id=1, article_id="art_1"))

    assert resp.content == "改写稿"  # disable 模式：原逻辑不变


def test_trace_llm_call_decorator_keeps_func_metadata():
    """functools.wraps：装饰后仍保留原函数名（日志 / 反射用）。"""
    assert TracedLLM.chat.__name__ == "chat"


async def test_trace_llm_call_enabled_reports_trace(fake_langfuse):
    llm = TracedLLM()

    await llm.chat(_req(user_id=42, article_id="art_1"))

    trace_kwargs = fake_langfuse.traces[0]
    assert trace_kwargs["name"] == "unit_chat"
    assert trace_kwargs["metadata"]["model"] == "claude-4-sonnet-20250514"
    assert trace_kwargs["metadata"]["user_id"] == 42


async def test_trace_llm_call_enabled_reports_generation(fake_langfuse):
    llm = TracedLLM()

    await llm.chat(_req(user_id=42, article_id="art_1"))

    span = fake_langfuse.trace_objects[0].spans[0]
    gen = span.generations[0]
    assert gen.name == "unit_chat"
    assert gen.kwargs["model"] == "claude-4-sonnet-20250514"
    assert gen.kwargs["output"] == "改写稿"
    assert gen.kwargs["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    assert span.kwargs["input"][0]["content"] == "请把这篇改写一下"


async def test_trace_llm_call_enabled_marks_error_and_reraises(fake_langfuse):
    llm = TracedLLM(raise_error=RuntimeError("LLM 炸了"))

    with pytest.raises(RuntimeError, match="LLM 炸了"):
        await llm.chat(_req(user_id=42))

    span_updates = fake_langfuse.trace_objects[0].spans[0].updates
    assert span_updates[-1]["level"] == "ERROR"
    assert span_updates[-1]["status_message"] == "LLM 炸了"


async def test_trace_llm_call_enabled_without_metadata(fake_langfuse):
    """没有 metadata 也不能崩（裸 /chat 调用）。"""
    llm = TracedLLM()

    resp = await llm.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert resp.content == "改写稿"
    assert fake_langfuse.traces[0]["metadata"]["model"] == "default"


# ---------------------------------------------------------------------------
# @trace_distill_step
# ---------------------------------------------------------------------------
@trace_distill_step("step1_structure")
async def traced_step(ctx, llm=None):
    return {"ok": True, "task_id": ctx.task_id}


@trace_distill_step("step1_structure")
async def traced_step_boom(ctx, llm=None):
    raise ValueError("step 炸了")


@trace_distill_step("step2_rewrite")
async def traced_step_long_output(ctx, llm=None):
    return "x" * 5000


async def test_trace_distill_step_disabled_runs_the_step(ctx, langfuse_disabled):
    result = await traced_step(ctx)

    assert result["ok"] is True
    assert result["task_id"] == ctx.task_id  # step 自己的返回值原样透传


async def test_trace_distill_step_enabled_reports_ctx_metadata(ctx, fake_langfuse):
    await traced_step(ctx)

    trace_kwargs = fake_langfuse.traces[0]
    assert trace_kwargs["name"] == "distill_step1_structure"
    assert trace_kwargs["metadata"]["task_id"] == ctx.task_id
    assert trace_kwargs["metadata"]["article_id"] == ctx.article_id
    assert trace_kwargs["metadata"]["user_id"] == ctx.user_id


async def test_trace_distill_step_enabled_records_truncated_output(ctx, fake_langfuse):
    await traced_step_long_output(ctx)

    updates = fake_langfuse.trace_objects[0].updates
    assert len(updates[0]["output"]) == 1000  # truncate 防正文打爆 trace


async def test_trace_distill_step_enabled_marks_error_and_reraises(ctx, fake_langfuse):
    with pytest.raises(ValueError, match="step 炸了"):
        await traced_step_boom(ctx)

    updates = fake_langfuse.trace_objects[0].updates
    assert updates[-1]["level"] == "ERROR"
    assert updates[-1]["status_message"] == "step 炸了"
