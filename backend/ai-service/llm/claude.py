"""Claude Sonnet 4 Client（v1 §5.2.3 Step 2 听感改写 + §5.4 Step 3 主题标签）。

本期（CP3.5-pre-1）只做接口签名 + httpx async + 指数退避 retry；
真 API 联调 / SSE 流式 / 精确 token 计数留 CP3.5。
"""
import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import LLMClient
from .exceptions import LLMError, RateLimitError
from .types import ChatRequest, ChatResponse, Usage


CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_DEFAULT_MODEL = "claude-4-sonnet-20250514"


class ClaudeSonnetClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str = CLAUDE_DEFAULT_MODEL,
        base_url: str = CLAUDE_API_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for ClaudeSonnetClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        start = time.time()
        model = req.model or self.model

        # Claude API 格式：system 字段单独，其他 messages
        system_msg = next((m.content for m in req.messages if m.role == "system"), None)
        non_system_msgs = [m for m in req.messages if m.role != "system"]

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in non_system_msgs],
        }
        if system_msg:
            body["system"] = system_msg
        if req.stop:
            body["stop_sequences"] = req.stop

        data = await self._post(body)

        content_blocks = data.get("content", [])
        content = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        usage_data = data.get("usage", {})
        stop_reason = data.get("stop_reason", "end_turn")

        return ChatResponse(
            content=content,
            model=model,
            usage=Usage(
                prompt_tokens=usage_data.get("input_tokens", 0),
                completion_tokens=usage_data.get("output_tokens", 0),
                total_tokens=usage_data.get("input_tokens", 0)
                + usage_data.get("output_tokens", 0),
            ),
            finish_reason="stop" if stop_reason == "end_turn" else "length",
            latency_ms=(time.time() - start) * 1000,
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST + 指数退避 retry（429 直接抛 RateLimitError，不重试）。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.post(self.base_url, json=body)
            except httpx.TimeoutException as e:
                last_err = e
            else:
                if resp.status_code == 429:
                    raise RateLimitError(
                        "claude rate limit", retry_after=resp.headers.get("retry-after")
                    )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    last_err = e
                else:
                    return resp.json()

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)  # 指数退避

        raise LLMError(f"claude chat failed after {self.max_retries} attempts: {last_err}")

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        # 真流式：CP3.5 接 SSE，本期只留接口
        raise NotImplementedError("ClaudeSonnetClient.stream 留 CP3.5 接 SSE")

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        # 真 token 计数：CP3.5 用 anthropic SDK 的 count_tokens
        # 本期 mock：len(text) // 4
        return len(text) // 4

    async def close(self) -> None:
        await self._client.aclose()
