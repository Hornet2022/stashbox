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
from .backoff import compute_backoff_seconds
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
        _shared: bool = False,  # CP3.6.2: 单例 client 关闭时跳过 httpx aclose
    ):
        if not api_key:
            raise ValueError("api_key required for OpenAIClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._shared = _shared
        self.timeout = timeout
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

        # CP3.6.2: prompt caching + tools 透传
        # 1. system 消息加 cache_control: ephemeral（OpenAI / Anthropic 都支持），
        #    命中后 system 部分 token 直降 ~75%（cache 命中价低于 input 价）。
        # 2. tools / tool_choice 按 OpenAI 协议透传。
        if messages and messages[0].get("role") == "system":
            messages[0]["cache_control"] = {"type": "ephemeral"}

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.stop:
            body["stop"] = req.stop
        if req.tools:
            body["tools"] = req.tools
        if req.tool_choice:
            body["tool_choice"] = req.tool_choice

        data = await self._post(body)

        message = data.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "")
        tool_calls_raw = message.get("tool_calls")  # 可能 None / [] / [list]
        usage_data = data.get("usage", {})
        finish_reason = data.get("choices", [{}])[0].get("finish_reason", "stop")

        # 解析 tool_calls → ChatResponse.tool_calls (list[ToolCall])
        from .types import ToolCall

        tool_calls: list[ToolCall] | None = None
        if tool_calls_raw:
            tool_calls = [
                ToolCall(
                    id=tc.get("id") or "",
                    type=tc.get("type", "function"),
                    function=tc.get("function", {}) or {},
                )
                for tc in tool_calls_raw
            ]
            # finish_reason 兼容：OpenAI 触发 tool_calls 时 finish_reason="tool_calls"

        return ChatResponse(
            content=content or "",
            model=model,
            usage=Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            latency_ms=(time.time() - start) * 1000,
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST + 指数退避 retry（含 429 —— 动作 3，2026-09-24）。

        修复：原实现只 catch TimeoutException 且最终错误吞掉 last_err（空串），
        导致真实失败原因（连接错误/超时/HTTP 错误体）无法排查。
        现统一捕获 TimeoutException + TransportError，并在末次 raise 时带 repr(last_err)。

        动作 3（2026-09-24）：**429 从「直接抛」改为纳入重试**。
        原设计对瞬时限流极不友好 —— 实测同一时刻单发 3/3 成功、并发才 429，
        一次 429 就让整条蒸馏失败。现在按指数退避 + 抖动 + 尊重 Retry-After
        重试；只有重试耗尽后仍限流才抛 RateLimitError（保留上游可识别语义）。
        """
        last_err: Exception | None = None
        rate_limited = False
        retry_after: str | None = None
        for attempt in range(self.max_retries):
            rate_limited = False
            retry_after = None
            try:
                # CP-LLM-DEADLINE：httpx 的 timeout 是**每次 I/O 操作**的超时，
                # 不是整个请求的总时长 —— 上游只要持续缓慢吐字节，读超时就会
                # 不断重置，请求可以无限期挂着。
                #
                # 实测踩过：蒸馏任务在 attempt=0 超时后就不再有任何日志，
                # worker 进程 CPU 0.3% / 状态 S / 日志 22 分钟零增长，
                # 一直挂到 Arq job_timeout 强杀为止。
                #
                # 这里套一层 asyncio.timeout 做**总墙钟死线**，与传输层超时正交，
                # 两者都超时才算失败。外层再乘 max_retries 得到单次 chat 的总预算。
                async with asyncio.timeout(self.timeout):
                    resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                log.warning("llm_request_transport_error", attempt=attempt, error=repr(e))
            else:
                if resp.status_code == 429:
                    rate_limited = True
                    retry_after = resp.headers.get("retry-after")
                    last_err = RateLimitError("openai rate limit", retry_after=retry_after)
                    log.warning(
                        "llm_request_rate_limited",
                        attempt=attempt,
                        retry_after=retry_after,
                    )
                else:
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
                delay = compute_backoff_seconds(
                    attempt, is_rate_limited=rate_limited, retry_after=retry_after
                )
                await asyncio.sleep(delay)

        # 重试耗尽：限流保留 RateLimitError 语义，其余归为 LLMError
        if rate_limited:
            raise RateLimitError("openai rate limit", retry_after=retry_after)
        raise LLMError(f"openai chat failed after {self.max_retries} attempts: {last_err!r}")

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """SSE 流式 chat/completions。"""
        try:
            model = req.model or self.model
            messages = [{"role": m.role, "content": m.content} for m in req.messages]
            # CP3.6.2: 流式也加 cache_control（system 消息时）
            if messages and messages[0].get("role") == "system":
                messages[0]["cache_control"] = {"type": "ephemeral"}
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "stream": True,
            }
            # tools 流式暂不暴露（先 chat，等响应稳定再扩）
            if req.tools:
                body["tools"] = req.tools
            if req.tool_choice:
                body["tool_choice"] = req.tool_choice

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
        # CP3.6.2：单例 client（factory `_shared=True`）→ no-op；
        # httpx 连接池由 `factory.close_all_llm_clients()` 在 lifespan shutdown 统一关闭。
        if not self._shared:
            await self._client.aclose()
