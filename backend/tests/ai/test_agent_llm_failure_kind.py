"""agent 侧的 LLM 失败分类（2026-11-09 内测事故）。

## 事故

上游 `tertu.cn` 整机不可达 → 4 篇内测文章全挂在 rewrite 步骤，
日志写 `agent distill failed at rewrite (timeout)`。

**"timeout" 是错的分类**：真实原因是**连不上**（ConnectTimeout）。
根因是 llm/openai.py 的总墙钟 `asyncio.timeout(...)` 抛的是裸 TimeoutError，
httpx 的真实异常被包进 `LLMError` 的**消息串**里，而旧分类只看 `str(e)`
→ "连不上"和"响应慢"混成一类 ⇒ 该熔断的不熔断 ⇒ 30 分钟空转。

## 本文件钉住的行为

- `LLMError(kind="unreachable")` ⇒ agent 的 `error_kind == "upstream_unreachable"`
- `LLMError(kind="timeout")` ⇒ `error_kind == "timeout"`（上游还活着，不熔断）
- `LLMCircuitOpen` ⇒ `error_kind == "upstream_unavailable"`，且 **retry_after 透传**
  （Arq 靠它把退避拉长到熔断窗口那么久，否则重试次数会被秒拒烧光）
- 旧的"消息里带 ReadTimeout"形态仍然判成 timeout（向后兼容）

**护栏价值**：把 `_classify_llm_failure` 退化成"只看 str(e)"，本文件立刻变红。
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

from agent import tools as tools_module  # noqa: E402
import llm as llm_module  # noqa: E402
from llm.exceptions import LLMCircuitOpen, LLMError  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_registry():
    tools_module._default_registry_singleton = None
    yield
    tools_module._default_registry_singleton = None


def _patch_llm(monkeypatch, exc: Exception):
    async def fake_fetch(state, args):
        return {"content": "内容", "meta": {}, "source_type": "web"}

    monkeypatch.setattr(tools_module, "fetch_url_tool", fake_fetch)

    class _FakeLLM:
        async def chat(self, req):
            raise exc

    monkeypatch.setattr(llm_module, "get_llm_client", lambda: _FakeLLM())


async def _run_agent():
    from agent.runner import agent_app

    return await agent_app.ainvoke(
        {
            "article_id": "art_t",
            "user_id": 1,
            "url": "https://x.com",
            "source": "web",
            "fetched_content": "内容",
        }
    )


@pytest.mark.asyncio
async def test_unreachable_kind_is_not_reported_as_timeout(monkeypatch):
    """🔴 护栏：连不上 ⇒ `upstream_unreachable`，**不能**是 timeout。"""
    err = LLMError("openai chat failed after 3 attempts: [unreachable] ConnectError()")
    err.kind = "unreachable"
    _patch_llm(monkeypatch, err)

    final = await _run_agent()

    assert final["status"] == "failed"
    assert final["error_kind"] == "upstream_unreachable", final
    assert final["error_step"] == "rewrite"


@pytest.mark.asyncio
async def test_timeout_kind_stays_timeout(monkeypatch):
    """响应慢 ⇒ 仍然是 timeout（上游还活着，不该熔断）。"""
    err = LLMError("openai chat failed after 3 attempts: [timeout] ReadTimeout('')")
    err.kind = "timeout"
    _patch_llm(monkeypatch, err)

    final = await _run_agent()

    assert final["error_kind"] == "timeout", final


@pytest.mark.asyncio
async def test_circuit_open_maps_to_upstream_unavailable_with_retry_after(monkeypatch):
    """🔴 护栏：熔断 ⇒ `upstream_unavailable`，且 retry_after 必须透传。

    retry_after 丢了的后果：Arq 固定 15s 退避，而熔断窗口 120s
    ⇒ 5 次重试全打在"拒绝服务"上，把重试次数烧光。
    """
    _patch_llm(monkeypatch, LLMCircuitOpen("circuit open", retry_after=97))

    final = await _run_agent()

    assert final["status"] == "failed"
    assert final["error_kind"] == "upstream_unavailable", final
    assert final.get("retry_after") == 97, final


@pytest.mark.asyncio
async def test_legacy_message_form_still_classified_as_timeout(monkeypatch):
    """向后兼容：旧形态（消息里带 ReadTimeout）仍然判 timeout。"""
    _patch_llm(monkeypatch, RuntimeError("openai chat failed after 3 attempts: ReadTimeout('')"))

    final = await _run_agent()

    assert final["error_kind"] == "timeout", final


def test_classify_llm_failure_finds_root_cause_through_chain():
    """🔴 护栏：根因在 `__cause__` 里也必须被挖出来。"""
    from agent.runner import _classify_llm_failure

    root = httpx.ConnectTimeout("timed out")
    wrapper = TimeoutError()
    wrapper.__cause__ = root

    kind, _retry_after = _classify_llm_failure(wrapper)
    assert kind == "upstream_unreachable"
