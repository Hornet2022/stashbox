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
from .exceptions import LLMCircuitOpen, LLMError, RateLimitError
from .failure import (
    KIND_UNKNOWN,
    classify_transport_error,
    is_unreachable,
)
from .types import ChatRequest, ChatResponse, Usage

log = structlog.get_logger(__name__)


OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

# ── 上游熔断（2026-11-09 事故引入）─────────────────────────────────────
#
# 起因（生产事故 2026-11-09）：上游 tertu.cn 整机不可达（TCP 连不上）。
# 而 LLM 客户端对每个请求都是「60s 总墙钟 × 3 次重试」，
# 外面再套 Arq 的「5 次任务重试」⇒ **单篇文章要等 30 分钟**才判失败，
# 而这 30 分钟里上游 100% 不可达 —— 每一次重试都是必然失败的空转。
#
# 熔断：连续 N 次**连不上**（unreachable）后开闸，窗口期内**不再发请求**，
# 直接抛 LLMCircuitOpen（带 retry_after）⇒
#   · 用户侧：从「等 30 分钟」变成「几秒内失败 + 明确原因」；
#   · 上游侧：不再被无意义的重试压住，给它喘息恢复的时间。
#
# 只对 unreachable 开闸，不对 timeout 开闸：响应慢说明上游**还活着**，
# 开闸会砍掉本可以成功的请求；连不上时每次请求都必然失败。
#
# 形状照抄 TTS 侧既有熔断器（app/services/tts/openai.py），
# 不另造一套并行体系。
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 120.0


# 「有 completion_tokens 但 content 为空」时的重试次数。
# 见 _openai_chat 里那段注释 —— 某些 OpenAI 兼容后端（实测 vLLM 0.22.0）
# 会偶发把生成内容丢出 content 字段，重发一次通常就好了。
# 3 次是实测里够用的量（实测 3 次里第 3 次就正常），再多就是浪费配额。
_EMPTY_CONTENT_RETRIES = 3


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
        connect_timeout: float = 10.0,
        disable_thinking: bool = True,
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
        self.connect_timeout = connect_timeout
        self.disable_thinking = disable_thinking
        # 熔断状态**挂在 client 实例上**（不是模块级全局）。
        #
        # 为什么：生产里 client 是 factory 的 lru_cache 单例
        # （同一份 provider/model/key/base_url ⇒ 同一实例）⇒ 熔断跨任务生效；
        # 而测试里每个用例新建 client ⇒ 状态天然隔离，不会互相污染。
        # 模块级全局会让"某个用例模拟了一次连接失败"把后面所有用例的
        # LLM 调用全部变成熔断拒绝（实测踩过）。
        self._circuit_state: dict[str, float] = {"consecutive_failures": 0, "opened_at": 0.0}
        self._client = httpx.AsyncClient(
            # 2026-11-09：连接超时**独立于**读超时。
            #
            # 事故形态：上游整机不可达（TCP 黑洞）⇒ httpx 拿 60s 的 timeout
            # 去等连接，每次尝试白等 60s，3 次重试 = 3 分钟才判死，
            # 外面再套 Arq 的 5 次任务重试 ⇒ 单篇文章等 30 分钟。
            #
            # "连不上"和"读得慢"是两件事：
            #   · 连不上 → TCP 要么秒通要么不通，给 10s 足够；
            #   · 连上了但回得慢 → 才需要 60s 的读超时。
            # 正常路径连接耗时是毫秒级，这个拆分对成功请求零影响。
            timeout=httpx.Timeout(
                timeout,
                connect=min(connect_timeout, timeout),
                read=timeout,
                write=timeout,
                pool=timeout,
            ),
            trust_env=False,  # 忽略沙箱/系统代理（漂移会打挂外网调用）
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    # ── 熔断（2026-11-09）──────────────────────────────────────────────
    #
    # 形状与 TTS 侧既有熔断器一致（app/services/tts/openai.py），
    # 差别是状态挂在**实例**上（理由见 __init__ 里的注释）。

    def _circuit_allow(self) -> bool:
        """当前是否允许发请求。

        关闭态 → 放行。
        打开态（距上次打开 < `_CIRCUIT_OPEN_SECONDS`）→ 拒绝。
        半开态（超过窗口）→ 放行（探路）。成功则完全恢复，失败则重新打开。
        """
        now = time.time()
        opened_at = self._circuit_state["opened_at"]
        if not opened_at:
            return True
        return (now - opened_at) >= _CIRCUIT_OPEN_SECONDS

    def _circuit_retry_after(self) -> int:
        """开闸后还剩多少秒（给 Arq 退避用，最少 1 秒）。"""
        remaining = _CIRCUIT_OPEN_SECONDS - (time.time() - self._circuit_state["opened_at"])
        return max(1, int(remaining))

    def _circuit_record_success(self) -> None:
        self._circuit_state["consecutive_failures"] = 0
        self._circuit_state["opened_at"] = 0.0

    def _circuit_record_failure(self) -> None:
        self._circuit_state["consecutive_failures"] += 1
        if self._circuit_state["consecutive_failures"] >= _CIRCUIT_FAIL_THRESHOLD:
            self._circuit_state["opened_at"] = time.time()
            log.warning(
                "llm_circuit_opened",
                consecutive_failures=self._circuit_state["consecutive_failures"],
                open_seconds=_CIRCUIT_OPEN_SECONDS,
            )

    def _reset_circuit_for_tests(self) -> None:
        """测试用：把熔断状态清回关闭态。"""
        self._circuit_state["consecutive_failures"] = 0
        self._circuit_state["opened_at"] = 0.0

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
        # CP-LLM-NO-THINK：关掉推理模型的思维链，否则长文改写必超总墙钟死线。
        #
        # 实测（doubao-seed-2.0-lite，2250 字输入 / 要求 3000 字输出）：
        #     基线                    106.4s  思考 4273 tok
        #     thinking.type=disabled   45.7s  思考    0 tok   ← 快 2.3 倍
        #     enable_thinking=False    76.4s  思考 2948 tok   （参数不被该网关识别）
        #     reasoning_effort=low    102.6s  思考 3616 tok   （基本没降）
        #
        # 蒸馏改写是"把长文转成播客稿"的生成任务，不是需要推理的任务；
        # 关掉思维链后输出长度和质量都没损失（4105 字 vs 基线 3800 字），
        # 但单次耗时从 100s+ 降到 45s 级别，给 300s 死线留出足够余量。
        #
        # 只认 `thinking: {"type": "disabled"}` —— 实测另两个参数名都不生效，
        # 写了也只是让 body 多两个被忽略的字段。
        if self.disable_thinking:
            body["thinking"] = {"type": "disabled"}

        data = await self._post(body)

        message = data.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "")
        tool_calls_raw = message.get("tool_calls")  # 可能 None / [] / [list]
        usage_data = data.get("usage", {})
        finish_reason = data.get("choices", [{}])[0].get("finish_reason", "stop")

        # ── 空 content 但确实生成了 token → 重试 ──────────────────────
        #
        # 实测（2026-10-21，tertu.cn 的 vLLM 0.22.0 后端）：
        # 同一个请求连发 3 次，前 2 次返回 content='' 但 usage.completion_tokens
        # 是 300~450 —— 模型**明明生成了内容**，只是没落到 content 字段里
        # （vLLM 偶发 detokenize 丢失）。第 3 次就正常返回 834 字的完整 JSON。
        #
        # 不做重试的后果（实测踩过）：content 空 → agent/runner.py 的
        # `getattr(resp, "content", None) or str(resp)` 回落到 str(resp) →
        # **把整个响应对象的 repr 当成了播客稿**，
        # 于是「听感整理稿」里出现
        #   content='' model='Qwen3.8-27B' usage=Usage(...) latency_ms=...
        # 这种天书，而文章状态还是「已就绪」、音频照常合成 —— 静默到没法发现。
        #
        # 判据用 completion_tokens > 0 而不是「content 为空」：
        # 真正合法的空回复（模型拒答/被截断）completion_tokens 也是 0，
        # 那种情况重试也没用，应该直接返回空让上层报错。
        if not content and usage_data.get("completion_tokens", 0) > 0:
            # ── 先试**流式**（2026-10-21 实测，这是根治手段）────────────────
            #
            # 实测（tertu.cn 的 vLLM 0.22.0 / Qwen3.8-27B，同一个 2925 字 prompt）：
            #     非流式 → 4/4 返回 content=""（而 finish_reason="stop"、
            #              completion_tokens 有值 —— 模型正常结束了却没写字）
            #     流式   → 0/4 空，内容 756~1018 字
            #
            # 上游非流式路径上「增量解码 → 累积进 content」这步有缺陷，
            # 而流式把累积交给客户端，正好绕开。所以**先试流式**，一次就够。
            #
            # 为什么放在非流式重试**之前**：非流式空 content 的概率实测约 40%
            # （长 prompt 更高），每次空都要等 4 次尝试 × ~8 秒 = 30 秒。
            # 先试流式则最坏情况也只多一次请求。
            try:
                log.warning(
                    "llm_empty_content_switch_to_stream model=%s completion_tokens=%s",
                    model,
                    usage_data.get("completion_tokens"),
                )
                data = await self._post_stream(body)
                message = data.get("choices", [{}])[0].get("message", {})
                content = message.get("content", "")
                tool_calls_raw = message.get("tool_calls")
                usage_data = data.get("usage", {}) or usage_data
                finish_reason = data.get("choices", [{}])[0].get("finish_reason", "stop")
            except Exception as exc:  # noqa: BLE001
                log.warning("llm_stream_fallback_failed model=%s error=%s", model, repr(exc))

            # 流式也空 → 退回常规非流式重试（上游偶发）
            for attempt in range(1, _EMPTY_CONTENT_RETRIES + 1):
                if content:
                    break
                log.warning(
                    "llm_empty_content_retry attempt=%s/%s model=%s completion_tokens=%s",
                    attempt,
                    _EMPTY_CONTENT_RETRIES,
                    model,
                    usage_data.get("completion_tokens"),
                )
                data = await self._post(body)
                message = data.get("choices", [{}])[0].get("message", {})
                content = message.get("content", "")
                tool_calls_raw = message.get("tool_calls")
                usage_data = data.get("usage", {})
                finish_reason = data.get("choices", [{}])[0].get("finish_reason", "stop")
            if not content:
                # 重试完还是空 —— 明确记一条，别让上层把 repr 当内容用
                log.error(
                    "llm_empty_content_after_retries model=%s completion_tokens=%s",
                    model,
                    usage_data.get("completion_tokens"),
                )

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

    async def _post_stream(self, body: dict[str, Any]) -> dict[str, Any]:
        """流式 POST，**客户端自己累积** delta.content。

        ## 为什么需要它

        实测（2026-10-21，tertu.cn 的 vLLM 0.22.0 / Qwen3.8-27B）：
        同一个 2925 字的 prompt，非流式 4/4 返回 `content=""`（而
        `finish_reason="stop"`、`completion_tokens` 有值 —— 模型**正常结束了**
        却一个字都没写进 content），**流式 0/4 空**。

        也就是说：上游非流式路径上「增量解码 → 累积进 content」这步有缺陷，
        而**流式把累积交给客户端**，正好绕开它。

        这也让长文不再需要拆段：2925 字的原文流式一次就过。

        返回值与非流式同构：{choices:[{message:{content, tool_calls}, finish_reason}],
        usage:{...}}，调用方无需区分。
        """
        import json as _json

        payload = {**body, "stream": True}
        # 流式下必须重新算总墙钟死线：httpx 的读超时是**每次 I/O 操作**的，
        # 上游持续吐字节会不断重置读超时 → 请求可能无限期挂着
        # （同 _post 里 CP-LLM-DEADLINE 记的那个坑）。
        timeout = httpx.Timeout(
            self.timeout,
            connect=min(self.connect_timeout, self.timeout),
            read=self.timeout,
        )
        content_parts: list[str] = []
        tool_calls_raw: Any = None
        finish_reason = "stop"
        usage_data: dict[str, Any] = {}
        last_err: Exception | None = None

        # 熔断闸门：流式与非流式走同一个上游，开闸期间同样不发请求。
        if not self._circuit_allow():
            wait_s = self._circuit_retry_after()
            log.warning("llm_circuit_open_fast_fail_stream", retry_after=wait_s)
            raise LLMCircuitOpen(
                f"openai upstream circuit open, retry after {wait_s}s",
                retry_after=wait_s,
            )

        for attempt in range(self.max_retries):
            content_parts = []
            tool_calls_raw = None
            finish_reason = "stop"
            usage_data = {}
            try:
                async with asyncio.timeout(self.timeout):
                    async with self._client.stream(
                        "POST", f"{self.base_url}/chat/completions", json=payload, timeout=timeout
                    ) as resp:
                        if resp.status_code >= 400:
                            body_text = (await resp.aread()).decode("utf-8", "replace")[:500]
                            raise RuntimeError(f"OpenAI 流式错误 {resp.status_code}: {body_text}")
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                if data == "[DONE]":
                                    break
                                continue
                            try:
                                obj = _json.loads(data)
                            except Exception:
                                continue
                            if obj.get("usage"):
                                usage_data = obj["usage"] or {}
                            for choice in obj.get("choices") or []:
                                delta = choice.get("delta") or {}
                                piece = delta.get("content")
                                if piece:
                                    content_parts.append(piece)
                                if delta.get("tool_calls"):
                                    tool_calls_raw = delta["tool_calls"]
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                if content_parts or usage_data.get("completion_tokens"):
                    break
                last_err = RuntimeError("流式响应没有任何内容")
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                if is_unreachable(e):
                    self._circuit_record_failure()
                log.warning(
                    "llm_stream_transport_error",
                    attempt=attempt,
                    kind=classify_transport_error(e),
                    error=repr(e),
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("llm_stream_error", attempt=attempt, error=repr(e))

        if not content_parts and last_err is not None:
            raise last_err

        return {
            "choices": [
                {
                    "message": {"content": "".join(content_parts), "tool_calls": tool_calls_raw},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage_data,
        }

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
        last_kind: str = KIND_UNKNOWN
        rate_limited = False
        retry_after: str | None = None

        # ── 熔断闸门（2026-11-09）────────────────────────────────────
        # 开闸期间**一个字节都不发**：直接抛 LLMCircuitOpen。
        # retry_after 让蒸馏层把 Arq 退避拉长到窗口那么久，
        # 免得 5 次重试全落在窗口内被秒拒（把重试次数烧光）。
        if not self._circuit_allow():
            wait_s = self._circuit_retry_after()
            log.warning("llm_circuit_open_fast_fail", retry_after=wait_s)
            raise LLMCircuitOpen(
                f"openai upstream circuit open, retry after {wait_s}s",
                retry_after=wait_s,
            )

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
                # 🔴 2026-11-09：分类必须看**根因**，不能看 str(e)。
                #    asyncio.timeout 到点抛的是裸 TimeoutError，真实原因
                #    （httpx.ConnectTimeout）在 __cause__ 里。
                #    只看 str(e) 会把"连不上"判成"超时" ⇒ 不熔断 ⇒ 空转 30 分钟。
                last_kind = classify_transport_error(e)
                if is_unreachable(e):
                    self._circuit_record_failure()
                log.warning(
                    "llm_request_transport_error",
                    attempt=attempt,
                    kind=last_kind,
                    error=repr(e),
                )
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
                        # 成功 ⇒ 熔断器复位（半开探路成功 = 上游已恢复）
                        self._circuit_record_success()
                        return resp.json()

            if attempt < self.max_retries - 1:
                delay = compute_backoff_seconds(
                    attempt, is_rate_limited=rate_limited, retry_after=retry_after
                )
                await asyncio.sleep(delay)

        # 重试耗尽：限流保留 RateLimitError 语义，其余归为 LLMError
        if rate_limited:
            raise RateLimitError("openai rate limit", retry_after=retry_after)
        err = LLMError(
            f"openai chat failed after {self.max_retries} attempts: " f"[{last_kind}] {last_err!r}"
        )
        # 带上机器可读的分类：上层（agent 分类 / 用户文案）读这个字段，
        # 不用再从消息里猜。
        err.kind = last_kind
        raise err

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
