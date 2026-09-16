"""LangfuseClient 单测（任务包 §4.1）。

全部用假 SDK（`fake_langfuse` fixture 替换 sys.modules["langfuse"]），不发真网络请求。
"""
import sys

import pytest

from observability.langfuse_client import LangfuseClient


# ---------------------------------------------------------------------------
# 默认（disable）模式
# ---------------------------------------------------------------------------
def test_disabled_is_default(monkeypatch, langfuse_disabled):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    client = LangfuseClient.get()

    assert client.enabled is False
    assert client._client is None  # 不构建 SDK


def test_disabled_methods_all_return_none(langfuse_disabled):
    client = LangfuseClient.get()

    assert client.create_trace("anything", metadata={"a": 1}) is None
    assert client.create_span(None, "span", input="x") is None
    assert client.create_generation(None, "gen", "mock-model") is None


def test_disabled_ignores_non_bool_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")  # 只有 "true" 才算开

    assert LangfuseClient.get().enabled is False


def test_enabled_flag_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "TRUE")

    assert LangfuseClient.get().enabled is True


# ---------------------------------------------------------------------------
# 启用模式
# ---------------------------------------------------------------------------
def test_enabled_builds_sdk_from_env(fake_langfuse):
    assert fake_langfuse.kwargs == {
        "public_key": "pk-lf-test",
        "secret_key": "sk-lf-test",
        "host": "https://langfuse.test",
    }


def test_enabled_create_trace_forwards_name_and_metadata(fake_langfuse):
    client = LangfuseClient.get()

    trace = client.create_trace("llm_chat", metadata={"model": "mock-model"})

    assert trace is not None
    assert fake_langfuse.traces[0]["name"] == "llm_chat"
    assert fake_langfuse.traces[0]["metadata"] == {"model": "mock-model"}


def test_enabled_create_trace_defaults_metadata_to_empty_dict(fake_langfuse):
    client = LangfuseClient.get()

    client.create_trace("llm_chat")

    assert fake_langfuse.traces[0]["metadata"] == {}


def test_enabled_create_span_and_generation_forward_args(fake_langfuse):
    client = LangfuseClient.get()
    trace = client.create_trace("llm_chat")

    span = client.create_span(trace, "llm_chat", input=[{"role": "user", "content": "hi"}])
    assert trace.spans[0].name == "llm_chat"
    assert span.kwargs["input"] == [{"role": "user", "content": "hi"}]

    gen = client.create_generation(
        span,
        name="llm_chat",
        model="mock-model",
        input="in",
        output="out",
        usage={"total_tokens": 3},
    )

    assert span.generations[0] is gen
    assert gen.kwargs["model"] == "mock-model"
    assert gen.kwargs["output"] == "out"
    assert gen.kwargs["usage"] == {"total_tokens": 3}


def test_create_span_returns_none_when_trace_is_none(fake_langfuse):
    client = LangfuseClient.get()

    assert client.create_span(None, "orphan") is None


def test_create_generation_returns_none_when_span_is_none(fake_langfuse):
    client = LangfuseClient.get()

    assert client.create_generation(None, "orphan", "mock-model") is None


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
def test_singleton_returns_same_instance(monkeypatch):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)

    first = LangfuseClient.get()
    assert LangfuseClient.get() is first


def test_reset_creates_fresh_instance(monkeypatch):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)

    first = LangfuseClient.get()
    LangfuseClient.reset()

    assert LangfuseClient.get() is not first


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------
def test_missing_sdk_downgrades_to_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "langfuse", None)  # import 会抛 ImportError

    client = LangfuseClient.get()

    assert client.enabled is False  # SDK 没装 → 不上报，不崩


def test_trace_failure_is_swallowed(monkeypatch, fake_langfuse):
    """Langfuse server down 只 log，不把异常抛进蒸馏主流程（任务包 §8）。"""

    def boom(**kwargs):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(fake_langfuse, "trace", boom)
    client = LangfuseClient.get()

    assert client.create_trace("llm_chat") is None


def test_generation_failure_is_swallowed(monkeypatch, fake_langfuse):
    client = LangfuseClient.get()
    trace = client.create_trace("llm_chat")
    span = client.create_span(trace, "llm_chat")

    def boom(**kwargs):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(span, "generation", boom)

    assert client.create_generation(span, "llm_chat", "mock-model") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
