"""空 content 的处理（2026-10-21 修的 vLLM 偶发 bug）。

## 事故

微信文章蒸馏后，「听感整理稿」里出现的是一段天书：

    content='' model='Qwen3.8-27B' usage=Usage(prompt_tokens=4791,
    completion_tokens=557, total_tokens=5348) finish_reason='stop'
    tool_calls=None latency_ms=12352.96...

而文章状态是「已就绪」、音频照常合成 —— **界面上完全看不出异常**。

## 根因（两层）

1. **服务端**：某些 OpenAI 兼容后端（实测 vLLM 0.22.0）会**偶发**返回
   ``content=''`` 但 ``usage.completion_tokens > 0`` —— 模型明明生成了
   内容，只是没落到 content 字段里。同一请求连发 3 次，实测有 2 次空、
   1 次正常 → 是概率性问题，不是必现。

2. **客户端**：`agent/runner.py` 的
   ``getattr(resp, "content", None) or str(resp)`` ——
   content 是空串（falsy）→ 回落到 ``str(resp)`` → **把响应对象的 repr
   当成了正文**。

## 修法

- ``llm/openai.py``：content 空但 completion_tokens > 0 → 自动重试（3 次）
- ``agent/runner.py``：不再用 ``str(resp)`` 兜底；content 为空就抛
  LLMError，走「LLM 返回空内容」的错误路径
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


def _load_runner():
    # runner.py 里有相对导入（from .state import ...），所以必须以
    # 「包内模块」的方式加载，不能用裸文件路径（那样没有 parent package）。
    import importlib

    if "agent" not in sys.modules:
        importlib.import_module("agent")
    return importlib.import_module("agent.runner")


runner = _load_runner()


class _FakeResponse:
    def __init__(self, content: str, model: str = "Qwen3.8-27B", completion_tokens: int = 0):
        self.content = content
        self.model = model
        self.finish_reason = "stop"
        self.usage = type(
            "U",
            (),
            {
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
                "total_tokens": 10 + completion_tokens,
            },
        )()


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, req):
        self.calls += 1
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_chat_text_returns_content_when_present():
    """正常情况：直接返回 content。"""
    client = _FakeClient([_FakeResponse("这是改写后的稿子。")])
    out = await runner._chat_text(client, "prompt", step="probe")
    assert out == "这是改写后的稿子。"


@pytest.mark.asyncio
async def test_chat_text_never_returns_repr_of_response():
    """content 为空 → 抛错，**绝不**返回 str(resp)。

    这是本次事故的核心断言：旧实现 `or str(resp)` 会把
    "content='' model='Qwen3.8-27B' usage=Usage(...)" 当成正文。
    """
    client = _FakeClient([_FakeResponse("", completion_tokens=557)])
    with pytest.raises(Exception) as exc:
        await runner._chat_text(client, "prompt", step="probe")
    msg = str(exc.value)
    assert "model=" not in msg, "错误信息里不能出现响应对象的 repr"
    assert "Usage(" not in msg


@pytest.mark.asyncio
async def test_chat_text_accepts_plain_string_response():
    """测试 mock 直接返回 str 的情况仍然支持。"""
    client = _FakeClient(["纯字符串响应"])
    out = await runner._chat_text(client, "prompt", step="probe")
    assert out == "纯字符串响应"


class TestEmptyContentRetry:
    """openai.py 的「空 content + 有 token → 重试」。"""

    @pytest.fixture
    def client_mod(self):
        # 同样是包内模块（有相对导入 from .base import ...）
        import importlib

        return importlib.import_module("llm.openai")

    @pytest.mark.asyncio
    async def test_retries_when_content_empty_but_tokens_generated(self, client_mod):
        """第 1 次空、第 2 次正常 → 应该重试并拿到第 2 次的结果。"""
        mod = client_mod
        payloads = [
            {
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 400},
            },
            {
                "choices": [{"message": {"content": "终于拿到了内容"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 380},
            },
        ]

        client = mod.OpenAIClient(api_key="k", base_url="http://x", model="m")
        calls = {"n": 0}

        async def _fake_post(body):
            r = payloads[min(calls["n"], len(payloads) - 1)]
            calls["n"] += 1
            return r

        client._post = _fake_post

        from llm.types import ChatRequest, ChatMessage

        req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
        resp = await client.chat(req)

        assert resp.content == "终于拿到了内容"
        assert calls["n"] == 2, "应该重试一次"

    @pytest.mark.asyncio
    async def test_no_retry_when_completion_tokens_zero(self, client_mod):
        """真正合法的空回复（没生成 token）→ 不浪费配额重试。"""
        mod = client_mod
        client = mod.OpenAIClient(api_key="k", base_url="http://x", model="m")
        calls = {"n": 0}

        async def _fake_post(body):
            calls["n"] += 1
            return {
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 0},
            }

        client._post = _fake_post

        from llm.types import ChatRequest, ChatMessage

        req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
        resp = await client.chat(req)

        assert resp.content == ""
        assert calls["n"] == 1, "没有 token 就不该重试（重试也没用）"
