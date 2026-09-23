"""CP3.5-pre-1 LLM 单测 fixture：把 ai-service 目录加进 sys.path。

ai-service 目录名带连字符（不是合法包名），`llm` 包只能这样被 import
（做法同 tests/observability、tests/gateway 按文件路径加载服务代码）。
"""

import os
import sys
from pathlib import Path

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, Feedback, User

AI_SERVICE_DIR = Path(__file__).resolve().parents[2] / "ai-service"

if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))


@pytest.fixture(autouse=True)
def _ensure_ffmpeg_on_path():
    """让 step4_concat 的 ffmpeg/ffprobe 子进程能找到二进制。

    本机 ffmpeg 安装在 /opt/homebrew/bin/，但 pytest 子进程未必继承；
    这里把常见 brew 位置 prepend 到 PATH，避免 FileNotFoundError。
    """
    extra = "/opt/homebrew/bin:/usr/local/bin"
    cur = os.environ.get("PATH", "")
    if "/opt/homebrew/bin" not in cur:
        os.environ["PATH"] = f"{extra}:{cur}"
    yield


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
    # CP7.3：埋点真落库后 feedback.user_id 的 FK 指向 users，直接 delete(user) 会
    # 触发 ForeignKeyViolationError —— 先清掉该用户的 feedback 行。
    await db_session.execute(delete(Feedback).where(Feedback.user_id == user.id))
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

    CP11.0.8 fixture 扩展：支持按 step 给不同 content（`step_contents` 参数）——
    蒸馏 4 步需要 Step1 结构化和 Step2 改写用不同响应，单一 content 不够。
    """

    def __init__(
        self,
        content: str = "mock response",
        *,
        raise_error: Exception | None = None,
        step_contents: dict[str, str] | None = None,
    ):
        self.content = content
        self.raise_error = raise_error
        self.step_contents = step_contents or {}
        self.requests: list = []

    def _resolve_content(self, req) -> str:
        """CP11.0.8: 按 step 返回不同 content。

        通过 req.metadata["step"] 判断（distill steps 都会设置 metadata["step"]）。
        """
        step = (req.metadata or {}).get("step")
        if step and step in self.step_contents:
            return self.step_contents[step]
        return self.content

    async def chat(self, req):
        self.requests.append(req)
        if self.raise_error is not None:
            raise self.raise_error
        from llm.types import ChatResponse, Usage

        resolved = self._resolve_content(req)
        return ChatResponse(
            content=resolved,
            model="fake-model",
            usage=Usage(prompt_tokens=len(req.messages[-1].content) // 4),
        )

    async def stream(self, req):
        yield self.content

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def close(self) -> None:
        return None


class FakeTTSClient:
    """返回真·静音 WAV bytes 的 TTS 替身（无网络，蒸馏单测用）。

    step3_tts 期望 synthesize 返回 bytes（真实 client 契约）；这里给一段
    合法 24k/16bit/mono 静音 WAV，让 step4_concat 走真实拼接路径、能算出时长，
    从而验证"非 mock"的蒸馏链路。
    """

    def __init__(self):
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)  # 16-bit
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000)  # 1 秒静音
        self._wav = buf.getvalue()

    async def synthesize(self, text: str) -> bytes:
        return self._wav

    @property
    def provider_name(self) -> str:
        return "fake"


@pytest.fixture
def fake_tts_cls():
    """返回 FakeTTSClient 类（测试里自己实例化）。"""
    return FakeTTSClient


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


@pytest.fixture(autouse=True)
def _refund_quota_no_redis_lock(monkeypatch):
    """CP9.x：pipeline._refund_quota 用 Redis SETNX 做幂等锁，
    但测试环境用同一 task_id 重复跑会跨测试状态污染。
    这里 mock 掉 redis_client.set 让每次 refund 都返回"未锁定"=可退。
    生产代码路径不变（仍会调真 Redis），只是测试里旁路。

    CP11.0.8 fixture 强化：_AlwaysUnlocked 接受任意构造参数 + async context manager 协议。
    解决跨 test 状态污染（缓存的 Redis client 跨 test 持有 _AlwaysUnlocked 引用）。
    """
    import redis.asyncio as redis_async

    class _AlwaysUnlocked:
        """接受任意构造参数 + async context manager 协议 + 任意 redis 命令。"""

        def __init__(self, *args, **kwargs):
            pass

        async def set(self, key, value, nx=False, ex=None):
            return True

        async def get(self, key):
            return None

        async def delete(self, *args, **kwargs):
            return 0

        async def aclose(self):
            return None

        async def ping(self):
            return True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        # 兜底：任何其他 redis 命令也安全 no-op
        async def __getattr__(self, name):
            async def _noop(*args, **kwargs):
                return None

            return _noop

    monkeypatch.setattr(redis_async, "Redis", _AlwaysUnlocked)
    yield


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
