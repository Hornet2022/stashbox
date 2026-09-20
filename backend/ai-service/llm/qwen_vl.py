"""Qwen VL Client（文本 + 多模态，用于 v1 §5.2.2 Step 1 内容结构化）。

Token Plan 团队版（2026-09-20）—— OpenAI 兼容端点：
`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions`

请求体格式（OpenAI 兼容）::

    {
        "model": "qwen3.6-flash",
        "messages": [{"role": "user", "content": "..."}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import LLMClient
from .exceptions import LLMError, RateLimitError
from .types import ChatRequest, ChatResponse, Usage


QWEN_VL_API_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
QWEN_VL_DEFAULT_MODEL = "qwen3.6-flash"


class QwenVLClient(LLMClient):
    """Qwen VL Client（文本 + 多模态，OpenAI 兼容 API）。

    用途：CP3.5 Step 1 - 内容结构化（v1 §5.2.2），接收文章截图 + 文本 → 输出 JSON 结构。
    base_url 指向 Token Plan 团队版 OpenAI 兼容端点（/chat/completions）。
    """

    def __init__(
        self,
        api_key: str,
        model: str = QWEN_VL_DEFAULT_MODEL,
        base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for QwenVLClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
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
            resp = await self._qwen_vl_chat(req)
            await self._maybe_trace(req, resp=resp)
            return resp
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def _qwen_vl_chat(self, req: ChatRequest) -> ChatResponse:
        start = time.time()
        model = req.model or self.model

        # OpenAI 兼容格式：content 可以是字符串或数组（多模态：text + image_url）
        messages = []
        for m in req.messages:
            if isinstance(m.content, str):
                messages.append({"role": m.role, "content": m.content})
            else:
                # m.content 是 str（不会走到这里，但类型是 str），直接放
                messages.append({"role": m.role, "content": m.content})

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }

        data = await self._post(body)

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage_data = data.get("usage", {})
        finish_reason = data.get("choices", [{}])[0].get("finish_reason", "stop")

        return ChatResponse(
            content=content,
            model=model,
            usage=Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            finish_reason=finish_reason,
            latency_ms=(time.time() - start) * 1000,
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST + 指数退避 retry（429 直接抛 RateLimitError，不重试）。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TimeoutException as e:
                last_err = e
            else:
                if resp.status_code == 429:
                    raise RateLimitError(
                        "qwen_vl rate limit", retry_after=resp.headers.get("retry-after")
                    )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    last_err = e
                else:
                    return resp.json()

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)  # 指数退避

        raise LLMError(f"qwen_vl chat failed after {self.max_retries} attempts: {last_err}")

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """SSE 流式 chat/completions，按 delta.content 增量 yield。"""
        try:
            model = req.model or self.model

            messages = []
            for m in req.messages:
                messages.append({"role": m.role, "content": m.content})

            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "stream": True,
            }

            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body
            ) as resp:
                if resp.status_code == 429:
                    raise RateLimitError(
                        "qwen_vl rate limit", retry_after=resp.headers.get("retry-after")
                    )
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    import json

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content

        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def close(self) -> None:
        await self._client.aclose()
