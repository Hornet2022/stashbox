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

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭底层资源（httpx session 等）。"""
        return None
