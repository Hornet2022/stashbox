"""LLM Client 抽象层（CP3.5-pre-1）。

设计原则：
1. async/await 一等公民（FastAPI 同步友好）
2. Pydantic v2 类型（与 v2 一致）
3. 不在 base 层做 retry / rate limit（留给具体实现）
4. 不在 base 层做 token counting（具体实现可挂 tiktoken / 官方 SDK）

具体实现：
- MockLLMClient：开发 + 单测用
- ClaudeSonnetClient：CP3.5 接真 API（v1 §5.2.3 Step 2 听感改写）
- QwenVLClient：CP3.5 接真 API（v1 §5.2.2 Step 1 内容结构化）

工厂模式：
- get_llm_client(model_name) 根据 model_name / provider 返回具体实现
- 默认走 MockLLMClient（开发环境 + 单测）
- LLM_PROVIDER=claude / qwen_vl / mock 切换（见 config_llm.py）
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from logging import getLogger

from .types import ChatRequest, ChatResponse


class LLMClient(ABC):
    """LLM Client abstract base class。"""

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """单次聊天（非流式）。

        Args:
            req: ChatRequest（含 messages / model / temperature / max_tokens / tools）

        Returns:
            ChatResponse（含 content / usage / model / finish_reason）

        Raises:
            RateLimitError: 触发速率限制
            TokenLimitError: 超过 max_tokens
            LLMError: 其他错误
        """
        ...

    @abstractmethod
    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """流式聊天（边生成边返）。

        Yields:
            str: 每个 chunk 的文本片段

        Raises:
            同 chat()
        """
        ...

    @abstractmethod
    async def count_tokens(self, text: str, model: str | None = None) -> int:
        """计算 token 数量（用于配额预估）。

        Mock 实现可以简单 len(text) // 4；真实现用 tiktoken 或官方 SDK。
        """
        ...

    async def _maybe_trace(
        self,
        req: ChatRequest,
        resp: ChatResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        """Langfuse 上报 + 成本归因钩子（CP3.5-pre-4）。

        具体实现（mock / claude / qwen_vl）在 chat/stream 的成功分支传 resp=，
        异常分支传 error=。

        LANGFUSE_ENABLED 默认 false —— 直接返回：不发 trace、不写 Redis，
        行为与 CP3.5-pre-2 完全一致（任务包 §4.5 向后兼容要求）。

        注：这里用顶层绝对 import（observability 与 llm 同级），同 distill/steps.py
        的 `from llm.types import ...` 约定。
        """
        from observability.langfuse_client import LangfuseClient

        client = LangfuseClient.get()
        if not client.enabled:
            return

        trace = client.create_trace(
            name="llm_chat",
            metadata={"model": resp.model if resp else req.model},
        )

        if resp is not None:
            messages = [m.model_dump() for m in req.messages]
            span = client.create_span(trace, "llm_chat", input=messages)
            client.create_generation(
                span,
                "llm_chat",
                model=resp.model,
                input=messages,
                output=resp.content,
                usage=resp.usage.model_dump(),
            )
            await self._record_cost(req, resp)
        elif error is not None and trace is not None:
            try:
                trace.update(level="ERROR", status_message=str(error))
            except Exception:  # 上报失败不能掩盖业务异常
                pass

    async def _record_cost(self, req: ChatRequest, resp: ChatResponse) -> None:
        """把 usage 折成 USD 记进 Redis（失败只 log 不抛）。"""
        from observability.cost_tracker import CostTracker

        metadata = req.metadata or {}
        user_id = metadata.get("user_id")
        article_id = metadata.get("article_id")
        if not user_id or not article_id:  # 蒸馏链路才归因（没有这两个 metadata 的裸调用跳过）
            return

        try:
            await CostTracker().record_llm_usage(
                user_id,
                article_id,
                resp.model,
                resp.usage.prompt_tokens,
                resp.usage.completion_tokens,
            )
        except Exception as exc:
            getLogger(__name__).warning("成本归因失败（忽略）: %s", exc)

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭底层资源（httpx session 等）。"""
        return None
