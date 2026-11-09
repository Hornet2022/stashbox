"""LLM 失败分类 + 上游熔断（2026-11-09 内测事故）。

## 事故

上游 `tertu.cn` 整机不可达（TCP 连不上），4 篇内测文章的蒸馏全部挂在
rewrite 步骤。日志写的是：

    error=TimeoutError()
    agent distill failed at rewrite (timeout): openai chat failed after 3 attempts: TimeoutError()

两个问题：

1. **分类错了**："连不上"被判成 "timeout" —— 因为 llm/openai.py 的总墙钟
   `asyncio.timeout(...)` 抛的是**裸 TimeoutError**，httpx 的真实异常
   （httpx.ConnectTimeout）只在 `__cause__` 里，而旧代码只读 `str(e)`。
2. **失败太慢**：60s 连接超时 × 3 次重试 × 5 次任务重试
   ⇒ 单篇文章要等 30 分钟才判失败，而这 30 分钟里上游 100% 不可达。

## 本文件钉住的行为

- 分类必须沿**异常链**找根因：ConnectTimeout ⇒ unreachable，ReadTimeout ⇒ timeout。
- 熔断：连续 unreachable 达阈值后开闸；开闸期间**一个请求都不发**。
- 连接超时独立于读超时（连不上 10s 判死，读得慢仍给 60s）。
- 半开：窗口过后放一个探路请求，成功即复位。

**护栏价值**：把 `classify_transport_error` 改成只看 `str(e)`，
或把熔断闸门删掉，本文件立刻变红。
"""

import sys
from pathlib import Path

import httpx
import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


# ── 分类 ────────────────────────────────────────────────────────────────


def test_connect_timeout_is_unreachable_not_timeout():
    """🔴 事故回归：连不上（ConnectTimeout）**不能**被判成 timeout。

    这正是 2026-11-09 事故里"分类错"的根因。
    """
    from llm.failure import KIND_TIMEOUT, KIND_UNREACHABLE, classify_transport_error

    err = httpx.ConnectTimeout("timed out")
    assert classify_transport_error(err) == KIND_UNREACHABLE
    assert classify_transport_error(err) != KIND_TIMEOUT


def test_connect_error_is_unreachable():
    from llm.failure import KIND_UNREACHABLE, classify_transport_error

    assert classify_transport_error(httpx.ConnectError("refused")) == KIND_UNREACHABLE


def test_read_timeout_is_timeout():
    """连上了但没在时限内读完 ⇒ timeout（上游还活着，不该熔断）。"""
    from llm.failure import KIND_TIMEOUT, classify_transport_error

    assert classify_transport_error(httpx.ReadTimeout("slow")) == KIND_TIMEOUT


def test_bare_timeout_error_is_timeout():
    from llm.failure import KIND_TIMEOUT, classify_transport_error

    assert classify_transport_error(TimeoutError()) == KIND_TIMEOUT


def test_root_cause_is_found_through_exception_chain():
    """🔴 核心回归：根因在 `__cause__` 里时必须被挖出来。

    asyncio.timeout 到点抛的裸 TimeoutError 把 httpx 的真实异常藏在
    `__cause__`；只读 str(e) 会把它误判成 timeout。
    """
    from llm.failure import KIND_UNREACHABLE, classify_transport_error

    root = httpx.ConnectTimeout("timed out")
    wrapper = TimeoutError()
    wrapper.__cause__ = root

    assert classify_transport_error(wrapper) == KIND_UNREACHABLE


def test_unknown_is_not_guessed_as_timeout():
    """认不出来就说认不出来 —— 猜成 timeout 会让"上游已死"被当成"上游慢"。"""
    from llm.failure import KIND_UNKNOWN, classify_transport_error

    assert classify_transport_error(ValueError("boom")) == KIND_UNKNOWN
    assert classify_transport_error(None) == KIND_UNKNOWN


def test_circuit_open_exception_carries_its_own_kind():
    """熔断异常自带 kind ⇒ 上层不用再猜。"""
    from llm.exceptions import LLMCircuitOpen
    from llm.failure import classify_transport_error

    exc = LLMCircuitOpen("circuit open", retry_after=42)
    assert classify_transport_error(exc) == "upstream_unavailable"
    assert exc.retry_after == 42


# ── 熔断 ────────────────────────────────────────────────────────────────


class _CountingTransport(httpx.AsyncBaseTransport):
    """记录请求次数的假 transport（不真的发网络请求）。"""

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


def _client_with_transport(transport, **kwargs):
    from llm.openai import OpenAIClient

    client = OpenAIClient(api_key="sk-test", base_url="https://example.invalid/v1", **kwargs)
    client._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0))
    return client


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_and_stops_sending_requests():
    """🔴 护栏：连不上达阈值后开闸；开闸期间**一个请求都不发**。

    这是"30 分钟空转"被治本的那一刀：不发请求 = 不浪费时间 = 用户秒失败。
    """
    from llm.exceptions import LLMCircuitOpen
    from llm.openai import _CIRCUIT_FAIL_THRESHOLD

    transport = _CountingTransport(error=httpx.ConnectError("refused"))
    client = _client_with_transport(transport, timeout=1.0, max_retries=1)

    # 连续失败到阈值 → 开闸
    for _ in range(_CIRCUIT_FAIL_THRESHOLD):
        with pytest.raises(Exception):
            await client._post({"model": "m", "messages": []})

    calls_before = transport.calls
    assert calls_before >= _CIRCUIT_FAIL_THRESHOLD

    # 开闸后：立即失败，且**不再发请求**
    with pytest.raises(LLMCircuitOpen) as excinfo:
        await client._post({"model": "m", "messages": []})
    assert transport.calls == calls_before, "熔断期间还在发请求 ⇒ 又变成 30 分钟空转"
    assert excinfo.value.retry_after and excinfo.value.retry_after > 0


@pytest.mark.asyncio
async def test_circuit_half_opens_after_window_and_success_resets():
    """窗口过后放一个探路请求；成功 ⇒ 熔断完全复位。"""
    import llm.openai as openai_mod

    transport = _CountingTransport(error=httpx.ConnectError("refused"))
    client = _client_with_transport(transport, timeout=1.0, max_retries=1)

    for _ in range(openai_mod._CIRCUIT_FAIL_THRESHOLD):
        with pytest.raises(Exception):
            await client._post({"model": "m", "messages": []})

    # 把窗口调到"已过期" ⇒ 半开
    client._circuit_state["opened_at"] = client._circuit_state["opened_at"] - (
        openai_mod._CIRCUIT_OPEN_SECONDS + 1
    )

    ok_transport = _CountingTransport()
    client._client = httpx.AsyncClient(transport=ok_transport, timeout=httpx.Timeout(5.0))
    data = await client._post({"model": "m", "messages": []})

    assert data["choices"][0]["message"]["content"] == "ok"
    assert client._circuit_state["consecutive_failures"] == 0
    assert client._circuit_state["opened_at"] == 0.0


@pytest.mark.asyncio
async def test_slow_upstream_does_not_open_circuit():
    """🔴 护栏：**响应慢**不熔断。

    慢说明上游还活着，开闸会砍掉本可以成功的请求。
    """
    import llm.openai as openai_mod

    transport = _CountingTransport(error=httpx.ReadTimeout("slow"))
    client = _client_with_transport(transport, timeout=1.0, max_retries=1)

    for _ in range(openai_mod._CIRCUIT_FAIL_THRESHOLD + 2):
        with pytest.raises(Exception):
            await client._post({"model": "m", "messages": []})

    assert client._circuit_state["opened_at"] == 0.0, "响应慢也熔断了 ⇒ 误伤"


@pytest.mark.asyncio
async def test_connect_timeout_is_shorter_than_read_timeout():
    """🔴 护栏：连接超时**独立于**读超时，且更短。

    连不上的主机要快速判死（10s），读得慢仍给足 60s。
    """
    from llm.openai import OpenAIClient

    client = OpenAIClient(api_key="sk-test", base_url="https://example.invalid/v1", timeout=60.0)
    timeout = client._client.timeout
    assert timeout.connect == pytest.approx(10.0)
    assert timeout.read == pytest.approx(60.0)
    await client._client.aclose()


@pytest.mark.asyncio
async def test_llm_error_carries_machine_readable_kind():
    """耗尽重试后抛的 LLMError 必须带 kind（上层据此分类 + 出文案）。"""
    from llm.exceptions import LLMError

    transport = _CountingTransport(error=httpx.ConnectError("refused"))
    client = _client_with_transport(transport, timeout=1.0, max_retries=2)

    with pytest.raises(LLMError) as excinfo:
        await client._post({"model": "m", "messages": []})

    assert excinfo.value.kind == "unreachable"
    assert "unreachable" in str(excinfo.value)


@pytest.mark.asyncio
async def test_circuit_state_is_per_client_instance():
    """🔴 护栏：熔断状态挂在**实例**上，不是模块级全局。

    模块级全局会让"某个用例模拟了一次连接失败"把后面所有用例的
    LLM 调用全部变成熔断拒绝（实测踩过：整份 test_llm_openai_tools_cache 变红）。
    """
    from llm.openai import OpenAIClient

    a = OpenAIClient(api_key="sk-test", base_url="https://example.invalid/v1")
    b = OpenAIClient(api_key="sk-test", base_url="https://example.invalid/v1")

    a._circuit_record_failure()
    assert a._circuit_state["consecutive_failures"] == 1
    assert b._circuit_state["consecutive_failures"] == 0, "两个 client 共享了熔断状态"

    await a._client.aclose()
    await b._client.aclose()
