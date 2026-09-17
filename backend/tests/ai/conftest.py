"""CP3.5-pre-1 LLM 单测 fixture：把 ai-service 目录加进 sys.path。

ai-service 目录名带连字符（不是合法包名），`llm` 包只能这样被 import
（做法同 tests/observability、tests/gateway 按文件路径加载服务代码）。
"""
import sys
from pathlib import Path

import uuid

import pytest
import pytest_asyncio

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, User

AI_SERVICE_DIR = Path(__file__).resolve().parents[2] / "ai-service"

if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))


@pytest_asyncio.fixture
async def db_session():
    """真 PostgreSQL session（content tests 同款：本机 5432 + alembic upgrade head）。"""
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session) -> int:
    """建一个测试用户（articles.user_id 外键指向 users.id）。"""
    user = User(
        open_id="cp3content_" + uuid.uuid4().hex[:24],
        nickname="pytest",
        tier="free",
        monthly_quota=5,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    yield int(user.id)
    await db_session.delete(user)
    await db_session.commit()


@pytest_asyncio.fixture
async def article_factory(db_session, test_user):
    """建 articles 行的工厂（raw_content 可传 None / dict）。"""

    async def _create(raw_content=None, **overrides):
        base = {
            "id": f"art_{uuid.uuid4().hex[:24]}",
            "user_id": test_user,
            "url": "https://example.com/x",
            "source": "wechat_mp",
            "title": "测试文章",
            "status": "pending",
            "raw_content": raw_content,
        }
        base.update(overrides)
        art = Article(**base)
        db_session.add(art)
        await db_session.commit()
        return art

    created: list = []

    async def _create_tracked(*args, **kwargs):
        art = await _create(*args, **kwargs)
        created.append(art)
        return art

    yield _create_tracked
    for art in created:
        await db_session.delete(art)
    await db_session.commit()


@pytest_asyncio.fixture
async def article_with_raw_content(article_factory):
    """默认 article：raw_content 是 CP-CREATE-ARTICLE 落库的 FetchResult。"""
    return await article_factory(
        {
            "content_text": "测试真内容 1+2=3",
            "title": "测试文章",
            "media_urls": ["https://example.com/img.jpg"],
            "source": "wechat_mp",
        }
    )


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
