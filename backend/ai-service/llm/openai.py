"""OpenAI 兼容 LLM 客户端（CP3.5-pre-1 + CP9.x）。

通用 OpenAI 协议客户端，可对接：
- OpenAI 官方：https://api.openai.com/v1
- Azure OpenAI：https://{r}.openai.azure.com/openai/deployments/{deployment}
- 任何 OpenAI-compatible 端点（含第三方代理的 Claude / Qwen 等）

请求体（OpenAI 兼容）::

    POST {base_url}/chat/completions
    Authorization: Bearer <api_key>
    {
        "model": "gpt-4o-mini",
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
import structlog

from .base import LLMClient
from .exceptions import LLMError, RateLimitError
from .types import ChatRequest, ChatResponse, Usage

log = structlog.get_logger(__name__)


OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIClient(LLMClient):
    """OpenAI 兼容 LLM 客户端（HTTP POST /chat/completions，Bearer auth）。

    用于 LLM_PROVIDER=openai，通过 OPENAI_LLM_BASE_URL 切底层 provider。
    """

    def __init__(
        self,
        api_key: str,
        model: str = OPENAI_DEFAULT_MODEL,
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for OpenAIClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,  # 忽略沙箱/系统代理（漂移会打挂外网调用）
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        try:
            resp = await self._openai_chat(req)
            await self._maybe_trace(req, resp=resp)
            return resp
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def _openai_chat(self, req: ChatRequest) -> ChatResponse:
        start = time.time()
        model = req.model or self.model

        messages = [{"role": m.role, "content": m.content} for m in req.messages]

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.stop:
            body["stop"] = req.stop

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
        """POST + 指数退避 retry（429 直接抛 RateLimitError，不重试）。

        修复：原实现只 catch TimeoutException 且最终错误吞掉 last_err（空串），
        导致真实失败原因（连接错误/超时/HTTP 错误体）无法排查。
        现统一捕获 TimeoutException + TransportError，并在末次 raise 时带 repr(last_err)。
        """
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                log.warning("llm_request_transport_error", attempt=attempt, error=repr(e))
            else:
                if resp.status_code == 429:
                    raise RateLimitError(
                        "openai rate limit", retry_after=resp.headers.get("retry-after")
                    )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    last_err = e
                    try:
                        body_preview = (await resp.aread()).decode("utf-8", "replace")[:500]
                    except Exception:
                        body_preview = "<unreadable response body>"
                    log.warning(
                        "llm_request_http_error",
                        attempt=attempt,
                        status=resp.status_code,
                        body=body_preview,
                    )
                else:
                    return resp.json()

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)

        raise LLMError(f"openai chat failed after {self.max_retries} attempts: {last_err!r}")

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """SSE 流式 chat/completions。"""
        try:
            model = req.model or self.model
            messages = [{"role": m.role, "content": m.content} for m in req.messages]
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
                        "openai rate limit", retry_after=resp.headers.get("retry-after")
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
