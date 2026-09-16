"""CP3.5-pre-1 LLM 单测 fixture：把 ai-service 目录加进 sys.path。

ai-service 目录名带连字符（不是合法包名），`llm` 包只能这样被 import
（做法同 tests/observability、tests/gateway 按文件路径加载服务代码）。
"""
import sys
from pathlib import Path

import pytest

AI_SERVICE_DIR = Path(__file__).resolve().parents[2] / "ai-service"

if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))


class FakeLLM:
    """返回固定内容的 LLMClient（CP3.5-pre-2 蒸馏单测用）。

    比 MockLLMClient 更可控：响应内容由测试直接指定，用来断言解析逻辑。
    """

    def __init__(self, content: str = "mock response", *, raise_error: Exception | None = None):
        self.content = content
        self.raise_error = raise_error
        self.requests: list = []

    async def chat(self, req):
        self.requests.append(req)
        if self.raise_error is not None:
            raise self.raise_error
        from llm.types import ChatResponse, Usage

        return ChatResponse(
            content=self.content,
            model="fake-model",
            usage=Usage(prompt_tokens=len(req.messages[-1].content) // 4),
        )

    async def stream(self, req):
        yield self.content

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def close(self) -> None:
        return None


def make_ctx(**overrides):
    """构造一个 DistillContext 测试样本。"""
    from distill import DistillContext

    base = {
        "task_id": "dst_test0000000000000000001",
        "article_id": "art_test000000000000000001",
        "user_id": 1,
        "url": "https://mp.weixin.qq.com/s/mock",
        "raw_content": "AI 行业最近发生了三件大事。第一，模型降价。第二，Agent 爆发。",
        "title": "AI 行业观察",
    }
    base.update(overrides)
    return DistillContext(**base)


@pytest.fixture(autouse=True)
def _langfuse_singleton():
    """每个用例前后清 LangfuseClient 单例（env 按用例切，不能跨泄漏）。"""
    from observability.langfuse_client import LangfuseClient

    LangfuseClient.reset()
    yield
    LangfuseClient.reset()


@pytest.fixture
def langfuse_disabled(monkeypatch):
    """显式关掉 Langfuse（默认模式）。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    return None


@pytest.fixture
def fake_langfuse(monkeypatch):
    """伪造 langfuse SDK，启用后所有上报落到内存对象上（不发真请求）。

    返回 FakeLangfuse 实例；走近 langfuse_client.LangfuseClient.get() 的正常 init 路径。
    """
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.test")

    import sys
    import types

    from observability import langfuse_client as pkg

    module = types.ModuleType("langfuse")
    instances: list = []

    class FakeSpan:
        def __init__(self, name, **kwargs):
            self.name = name
            self.kwargs = kwargs
            self.updates: list[dict] = []
            self.generations: list = []

        def generation(self, **kwargs):
            gen = FakeSpan(kwargs.pop("name", ""), **kwargs)
            self.generations.append(gen)
            return gen

        def update(self, **kwargs):
            self.updates.append(kwargs)
            return None

    class FakeTrace(FakeSpan):
        def __init__(self, name, **kwargs):
            super().__init__(name, **kwargs)
            self.spans: list = []

        def span(self, **kwargs):
            span = FakeSpan(kwargs.pop("name", ""), **kwargs)
            self.spans.append(span)
            return span

    class FakeLangfuse:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.traces: list[dict] = []
            self.trace_objects: list = []
            instances.append(self)

        def trace(self, **kwargs):
            trace = FakeTrace(kwargs.pop("name", ""), **kwargs)
            self.traces.append({**kwargs, "name": trace.name})
            self.trace_objects.append(trace)
            return trace

        def flush(self):  # v2 SDK 有，留个桩
            return None

    module.Langfuse = FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)

    client = pkg.LangfuseClient.get()
    assert client.enabled is True
    return client._client


@pytest.fixture
def fake_llm_cls():
    """返回 FakeLLM 类（测试里自己传 content / raise_error）。"""
    return FakeLLM


@pytest.fixture
def ctx():
    """一个默认 DistillContext；需要改字段时用 make_ctx(...)。"""
    return make_ctx()
