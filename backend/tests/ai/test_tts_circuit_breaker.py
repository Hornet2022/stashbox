"""TTS 熔断器（2026-10-22 上游 TTS 100% 504 事故）。

## 事故

上游（tertu.cn 的 Qwen3-TTS）有 **60s 墙钟**且是**共享**资源。
2026-10-22 07:44~07:56 我连续打 500~3000 字的大请求把上游压满，
之后 30+ 分钟上游 **100% 504**（连续 87/91 次），
**3 篇真实用户的剪藏全部失败**（job_try 打满 max_tries）。

同期对照实验证明这不是「我压测的余波」：
停掉自己的 worker、排空队列后，单发一个 **20 字** 的请求仍然是
`504 上游语音合成超时 (60s)`，而**同一个 host** 的 LLM 端点
1.05s 返回 200 —— 所以是上游 TTS 侧的故障，不是我们的网络/密钥问题。

## 为什么必须有熔断

上游一慢，我们每个请求都白等 60s，然后退避重试、再白等 60s。
这不是重试，是**加压** —— 而且 Arq 的重试次数（5 次）会在
上游恢复之前全部烧光，用户的文章被判死。

熔断打开后**立刻失败**（不占上游 60s），把重试节奏交给退避，
给上游留出恢复窗口。

## 契约

- 连续失败 N 次 → 打开（拒绝一切请求）
- 窗口期满 → 半开，放行（探路）；成功则完全恢复，失败则重新打开
- **4xx 不计入失败**（音色名写错是我们自己的请求有问题，
  重试一万次也是同样的错，而且它不代表上游不可用）
- 熔断异常带 `retry_after`（= 熔断窗口），蒸馏层据此拉长退避
"""

import sys
from pathlib import Path

import httpx
import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def openai_mod():
    from app.services.tts import openai as mod

    return mod


@pytest.fixture(autouse=True)
def _reset_circuit(openai_mod):
    """每个用例前后都复位熔断状态（它是模块级全局，跨用例会串）。"""
    openai_mod._circuit_state["consecutive_failures"] = 0
    openai_mod._circuit_state["opened_at"] = 0.0
    yield
    openai_mod._circuit_state["consecutive_failures"] = 0
    openai_mod._circuit_state["opened_at"] = 0.0


class _FakeResp:
    def __init__(self, status_code: int = 200, content: bytes = b"a" * 256):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "audio/mpeg"}

    def json(self):
        return {"error": {"code": "x", "message": "y"}}


class _FakeHTTP:
    """只实现 post 的假 httpx client，按脚本回放响应/异常。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0

    async def post(self, url, json=None):  # noqa: A002 - 与 httpx 签名一致
        self.calls += 1
        item = self.script.pop(0) if self.script else _FakeResp()
        if isinstance(item, Exception):
            raise item
        return item


def _client(openai_mod, script: list):
    client = openai_mod.OpenAITTSClient(
        api_key="k", base_url="http://x", model="m", chunk_retries=0
    )
    client._client = _FakeHTTP(script)
    return client


def _timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("boom")


@pytest.mark.asyncio
async def test_three_consecutive_failures_open_the_circuit(openai_mod):
    """连续失败到阈值 → 熔断打开，后续请求**不再打端点**。"""
    client = _client(openai_mod, [_timeout(), _timeout(), _timeout()])

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")

    assert openai_mod._circuit_state["consecutive_failures"] == 3
    assert openai_mod._circuit_state["opened_at"] > 0

    # 熔断打开后：立刻失败，且**一次网络请求都不发**
    calls_before = client._client.calls
    with pytest.raises(openai_mod._CircuitOpen):
        await client._synthesize_once("你好。")
    assert client._client.calls == calls_before, "熔断期间不该再打上游"


@pytest.mark.asyncio
async def test_circuit_open_exception_carries_retry_after(openai_mod):
    """熔断异常必须带 retry_after —— 蒸馏层靠它把退避拉长到熔断窗口。"""
    client = _client(openai_mod, [_timeout(), _timeout(), _timeout()])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")

    with pytest.raises(openai_mod._CircuitOpen) as excinfo:
        await client._synthesize_once("你好。")

    assert excinfo.value.retry_after == openai_mod._CIRCUIT_OPEN_SECONDS
    # 退避必须 ≥ 熔断窗口，否则 Arq 的重试会全打在「拒绝服务」上
    assert excinfo.value.retry_after >= 60


@pytest.mark.asyncio
async def test_success_resets_the_failure_counter(openai_mod):
    """成功一次就清零 —— 上游恢复后要能立刻回到正常状态。"""
    client = _client(
        openai_mod,
        [_timeout(), _timeout(), _FakeResp(), _timeout(), _timeout(), _FakeResp()],
    )

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")
    await client._synthesize_once("你好。")

    assert openai_mod._circuit_state["consecutive_failures"] == 0
    assert openai_mod._circuit_state["opened_at"] == 0.0

    # 清零后再失败两次也不该熔断
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")
    assert openai_mod._circuit_state["opened_at"] == 0.0


@pytest.mark.asyncio
async def test_client_error_does_not_open_the_circuit(openai_mod):
    """4xx（音色名写错、参数非法）**不**算上游故障，不该熔断。

    上游的报错原文就是 400：
        Invalid voice '__nope__'. Supported: aiden, dylan, eric, ...
    这是我们的请求有问题，重试一万次也是同样的错；
    而且它完全不代表上游不可用 —— 把它算进熔断会误伤正常流量。
    """
    client = _client(openai_mod, [_FakeResp(status_code=400) for _ in range(10)])

    for _ in range(5):
        with pytest.raises(RuntimeError, match="400"):
            await client._synthesize_once("你好。")

    assert openai_mod._circuit_state["consecutive_failures"] == 0
    assert openai_mod._circuit_state["opened_at"] == 0.0
    # 5 次都真的打了端点（没有被熔断拦掉）
    assert client._client.calls == 5


@pytest.mark.asyncio
async def test_server_error_does_open_the_circuit(openai_mod):
    """5xx 是上游扛不住 → 计入失败，熔断该打开。"""
    client = _client(openai_mod, [_FakeResp(status_code=503) for _ in range(5)])

    for _ in range(3):
        with pytest.raises(RuntimeError, match="503"):
            await client._synthesize_once("你好。")

    assert openai_mod._circuit_state["opened_at"] > 0


@pytest.mark.asyncio
async def test_circuit_half_opens_after_the_window(openai_mod):
    """窗口期满 → 放行探路；探路成功 → 完全恢复。"""
    client = _client(openai_mod, [_timeout(), _timeout(), _timeout()])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")

    # 把打开时间往前挪，模拟窗口已过
    openai_mod._circuit_state["opened_at"] -= openai_mod._CIRCUIT_OPEN_SECONDS + 1

    assert openai_mod._circuit_allow() is True
    await client._synthesize_once("你好。")  # 探路成功
    assert openai_mod._circuit_state["consecutive_failures"] == 0
    assert openai_mod._circuit_state["opened_at"] == 0.0


@pytest.mark.asyncio
async def test_chunk_synthesis_does_not_retry_while_circuit_is_open(openai_mod):
    """熔断打开时，整篇分块合成**一次请求都不发**（重试只会继续加压）。

    断言放在 HTTP 层：`_synthesize_once` 内部的熔断闸门是真实生效的，
    端点压根没被碰到 —— 这正是「快速失败」的定义。
    """
    client = _client(openai_mod, [_timeout(), _timeout(), _timeout()])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client._synthesize_once("你好。")

    calls_before = client._client.calls

    with pytest.raises(openai_mod._CircuitOpen):
        await client.synthesize("第一句。第二句。第三句。" * 20)

    assert client._client.calls == calls_before, "熔断期间不该再打上游"


# ---------------------------------------------------------------------------
# retry_after 的传递链：熔断 → ToolError → AgentState → 蒸馏退避
# ---------------------------------------------------------------------------


def test_retry_after_is_found_through_the_exception_chain():
    """retry_after 要能穿过 ToolError / RuntimeError 的层层包装。

    熔断异常在 TTS client 抛出，中间被 ToolError、RuntimeError 包了两次，
    只有**链底**那个还带着 `retry_after` 属性。
    """
    import sys
    from pathlib import Path

    ai_service = Path(__file__).resolve().parents[2] / "ai-service"
    if str(ai_service) not in sys.path:
        sys.path.insert(0, str(ai_service))

    from tasks.distill_task import _retry_after_seconds

    class _Inner(RuntimeError):
        retry_after = 60.0

    inner = _Inner("熔断")
    middle = RuntimeError("agent 失败")
    middle.__cause__ = inner
    outer = RuntimeError("蒸馏失败")
    outer.__cause__ = middle

    assert _retry_after_seconds(outer) == 60
    # 没有 retry_after 时用默认值
    assert _retry_after_seconds(RuntimeError("普通失败")) == 15
    # 字符串形式（ToolError 里是 str）也要认
    inner2 = _Inner("熔断")
    inner2.retry_after = "45"
    assert _retry_after_seconds(inner2) == 45
