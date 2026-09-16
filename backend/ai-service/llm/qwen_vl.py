"""Qwen2.5-VL Client（多模态，用于 v1 §5.2.2 Step 1 内容结构化）。

DashScope 请求体（CP3.5 接真 API 时按此形状组装）::

    {
        "model": "qwen2.5-vl-72b-instruct",
        "input": {"messages": [{"role": "user", "content": [...]}]},
        "parameters": {"temperature": ..., "max_tokens": ...},
    }
"""
from collections.abc import AsyncIterator

import httpx

from .base import LLMClient
from .types import ChatRequest, ChatResponse


QWEN_VL_API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
QWEN_VL_DEFAULT_MODEL = "qwen2.5-vl-72b-instruct"


class QwenVLClient(LLMClient):
    """Qwen2.5-VL 多模态 Client（图片理解 + 文本生成）。

    用途：CP3.5 Step 1 - 内容结构化（v1 §5.2.2），接收文章截图 + 文本 → 输出 JSON 结构。
    本期只做接口签名 + 鉴权 header；chat/stream 留 CP3.5 接 DashScope API。
    """

    def __init__(
        self,
        api_key: str,
        model: str = QWEN_VL_DEFAULT_MODEL,
        base_url: str = QWEN_VL_API_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for QwenVLClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        try:
            raise NotImplementedError("QwenVLClient.chat 留 CP3.5 接 DashScope API")
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        try:
            raise NotImplementedError("QwenVLClient.stream 留 CP3.5")
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def close(self) -> None:
        await self._client.aclose()
