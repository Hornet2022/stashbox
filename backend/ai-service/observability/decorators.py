"""Langfuse 上报装饰器（CP3.5-pre-4）。

两个装饰器：
- `@trace_llm_call(name)`：装饰 LLMClient.chat/stream，上报 model + usage + output
- `@trace_distill_step(name)`：装饰蒸馏 4 步，上报 task_id / article_id / user_id

LANGFUSE_ENABLED=false（默认）时两个装饰器都是「只跑原逻辑」的薄壳，无副作用。
"""
import functools
from typing import Any

from .langfuse_client import LangfuseClient

_TRACE_OUTPUT_MAX_LEN = 1000


def _messages_of(req: Any) -> list[dict]:
    return [m.model_dump() for m in req.messages] if hasattr(req, "messages") else []


def _update(observation: Any, **fields) -> None:
    if observation is None:
        return
    try:
        observation.update(**fields)
    except Exception:  # 上报失败不能掩盖业务异常
        pass


def trace_llm_call(name: str | None = None):
    """装饰 LLMClient.chat，自动上报 Langfuse trace / span / generation。

    用法::

        @trace_llm_call("claude_chat")
        async def chat(self, req: ChatRequest) -> ChatResponse: ...
    """

    def decorator(func):
        trace_name = name or func.__name__

        @functools.wraps(func)
        async def wrapper(self, req, *args, **kwargs):
            client = LangfuseClient.get()
            metadata = (getattr(req, "metadata", None) or {}).copy()
            metadata.setdefault("model", req.model or "default")

            trace = client.create_trace(name=trace_name, metadata=metadata)
            span = client.create_span(trace, name=trace_name, input=_messages_of(req))

            try:
                resp = await func(self, req, *args, **kwargs)
            except Exception as exc:
                _update(span, level="ERROR", status_message=str(exc))
                _update(trace, level="ERROR", status_message=str(exc))
                raise

            client.create_generation(
                span,
                name=trace_name,
                model=resp.model,
                input=_messages_of(req),
                output=resp.content,
                usage={
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                },
            )
            return resp

        return wrapper

    return decorator


def trace_distill_step(step_name: str):
    """装饰蒸馏 step 函数，上报 trace_id / article_id / user_id。

    用法::

        @trace_distill_step("step1_structure")
        async def step1_structure(ctx: DistillContext, llm) -> None: ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            client = LangfuseClient.get()
            trace = client.create_trace(
                name=f"distill_{step_name}",
                metadata={
                    "task_id": getattr(ctx, "task_id", None),
                    "article_id": getattr(ctx, "article_id", None),
                    "user_id": getattr(ctx, "user_id", None),
                },
            )

            try:
                result = await func(ctx, *args, **kwargs)
            except Exception as exc:
                _update(trace, level="ERROR", status_message=str(exc))
                raise

            _update(trace, output=str(result)[:_TRACE_OUTPUT_MAX_LEN])
            return result

        return wrapper

    return decorator
