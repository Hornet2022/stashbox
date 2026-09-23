"""Arq worker task 单测（任务包 §4.2）。

两层覆盖：
- 用 FakePipeline 隔离测「参数 → DistillContext」适配 + 返回值 + 异常传播
- 用真 DistillPipeline + 假 session factory 测端到端（失败退配额 / DB 状态写回）
"""

import pytest
from contextlib import asynccontextmanager
from structlog.testing import capture_logs

from tasks import distill_task as dt_module
from distill import DistillPipeline, DistillStatus
from stashbox.backend.common import quota_service

# conftest.py 暴露的 FakeLLM 通过 `fake_llm_cls` fixture 拿，pytest 不支持 from conftest import。
# 这里在 fake_pipeline fixture 内 inline 创建一个最小 FakeLLM 替身（足够 distill_task 不跑真 LLM）。


class FakePipeline:
    """替身：不跑 4 步，只记录 kwargs / ctx，可选抛异常。"""

    instances: list["FakePipeline"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.ctx = None
        FakePipeline.instances.append(self)

    async def run(self, ctx):
        self.ctx = ctx
        return ctx


class FailingPipeline(FakePipeline):
    async def run(self, ctx):
        self.ctx = ctx
        raise RuntimeError("step2 boom")


@pytest.fixture(autouse=True)
def _reset_fake_pipeline():
    FakePipeline.instances.clear()
    yield
    FakePipeline.instances.clear()


@pytest.fixture
def fake_pipeline(monkeypatch):
    """CP11.0.8 fixture 同步：distill_task 现在走「CP-DELETE 兜底」调真 AsyncSessionLocal() 查 article 存在性。

    这里同时 patch AsyncSessionLocal → 一个 async context manager 函数 + FakePipeline + LLM 替身，
    否则测试 fixture 环境没真 PG / 没 article 行时，CP-DELETE 兜底会 skipped_article_missing 早退，
    FakePipeline.instances 是空，下游所有断言（ctx, kwargs, ctx.title）全失败。

    返回值：FakePipeline 类（测试用 FakePipeline.instances[0].ctx 断言）。
    """
    _shared_setup(monkeypatch, FakePipeline)
    return FakePipeline


@pytest.fixture
def fake_pipeline_factory(monkeypatch):
    """CP11.0.8 fixture 扩展：端到端测试用（真 DistillPipeline + 可断言的 fake factory）。

    返回 factory —— 测试用 factory.statuses / factory.statements 断言。
    与 fake_pipeline 的区别：fake_pipeline 替换 DistillPipeline=FakePipeline（隔离测试），
    fake_pipeline_factory 替换 DistillPipeline=DistillPipeline（端到端测试）。
    """
    factory = _shared_setup(monkeypatch, DistillPipeline)
    return factory


def _shared_setup(monkeypatch, pipeline_cls):
    """CP11.0.8 抽出的 setup helper：fake_pipeline + fake_pipeline_factory 共用。"""

    class _FakeLLM:
        async def chat(self, req):
            from llm.types import ChatResponse, Usage

            step = (req.metadata or {}).get("step")
            if step == "step1_structure":
                content = (
                    '{"summary":"x",'
                    '"chapters":[{"title":"章1","summary":"","key_points":[],"quotes":[],"tension":""}],'
                    '"entities":[],"tags":[]}'
                )
            elif step == "step2_rewrite":
                content = (
                    '{"hook":"开场钩子。",'
                    '"sections":["主体第一段内容。"],'
                    '"outro":"结尾钩子。",'
                    '"word_count":15}'
                )
            else:
                content = "{}"

            return ChatResponse(
                content=content,
                model="fake",
                usage=Usage(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(dt_module, "DistillPipeline", pipeline_cls)
    factory = FakeSessionFactory()

    @asynccontextmanager
    async def _fake_async_session_local():
        session = factory()
        yield session

    monkeypatch.setattr(dt_module, "AsyncSessionLocal", _fake_async_session_local)
    monkeypatch.setattr(dt_module, "get_llm_client", lambda *a, **kw: _FakeLLM())
    return factory


@pytest.fixture(autouse=True)
def raw_content_calls(monkeypatch):
    """把 _load_raw_content 打成替身（CP3-CONTENT 后它要查真 DB）。

    本文件测的是「任务参数 → DistillContext 适配 / 异常传播」，不是 DB 读取；
    DB 读取本身由 test_distill_task_content.py 用真 PostgreSQL 覆盖。
    返回记录的 article_id 列表，供用例断言 loader 确实被调用。
    """
    calls: list = []

    async def _load(db, article_id):
        calls.append(article_id)
        return f"[stub raw content] article_id={article_id}"

    monkeypatch.setattr(dt_module, "_load_raw_content", _load)
    return calls


class FakeSession:
    """记录 execute() 收到的语句，不做真实 DB IO。

    CP11.0.8 fixture 同步：distill_task.py 加了「CP-DELETE 兜底」调
    `db.scalar(select(Article.id).where(...))` 查 article 存在性。
    这里 mock 出 `scalar()` 方法，默认返回 article 存在（= "art_1"），
    可通过 `article_exists=False` 让对应测试走 skipped_article_missing 分支。
    """

    def __init__(self, recorder: list, *, article_exists: bool = True):
        self.recorder = recorder
        self.article_exists = article_exists

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.recorder.append(stmt)
        # CP11.0.8 fixture 同步：distill_task 后续会 `result.scalar_one_or_none()` 读 Article / DistilledArticle。
        # 返回一个简单 Result mock：scalar_one_or_none 永远返回 None（article 不存在 = 无副作用）。
        return _FakeResult()

    async def scalar(self, stmt):
        """CP-DELETE 兜底 mock（FakeSession.scalar）：返回 article.id（存在）或 None（不存在）。

        distill_task.py:204 调 `await db.scalar(select(Article.id).where(Article.id == article_id))`。
        """
        # 简化：所有 scalar 查询都按 article_exists 配置返回，不区分 stmt
        return "art_1" if self.article_exists else None

    async def commit(self):
        return None

    async def begin_nested(self):
        """CP6.2.2.2b 埋点用：analytics.track 内部 savepoint 走 session.begin_nested()。"""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _savepoint():
            yield self

        return _savepoint()


class _FakeResult:
    """CP11.0.8 fixture 同步：让 execute 返回值支持 SQLAlchemy Result 接口。"""

    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class FakeSessionFactory:
    def __init__(self, *, article_exists: bool = True):
        self.statements: list = []
        self.article_exists = article_exists

    def __call__(self) -> FakeSession:
        return FakeSession(self.statements, article_exists=self.article_exists)

    @property
    def statuses(self) -> list[str]:
        out = []
        for stmt in self.statements:
            for column, value in getattr(stmt, "_values", {}).items():
                if column.key == "status":
                    out.append(value.value if hasattr(value, "value") else value)
        return out


async def _noop_refund(session, user_id, amount=1):
    """不碰 DB 的 refund 替身（真 refund 会查 users 表）。"""
    return {}


CTX = {"job_id": "job-1", "redis": None}


# ---------------------------------------------------------------------------
# 成功路径（FakePipeline 隔离）
# ---------------------------------------------------------------------------
async def test_success_returns_done(fake_pipeline):
    result = await dt_module.distill_task(
        CTX, "dst_1", "art_1", 7, "https://mp.weixin.qq.com/s/x", title="标题"
    )

    assert result == {"task_id": "dst_1", "status": "done"}


async def test_task_builds_distill_context_from_args(fake_pipeline):
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a", title="标题")

    ctx = FakePipeline.instances[0].ctx
    assert ctx.task_id == "dst_1"
    assert ctx.article_id == "art_1"
    assert ctx.user_id == 7
    assert ctx.url == "https://x.com/a"
    assert ctx.title == "标题"


async def test_task_loads_raw_content_from_db(fake_pipeline, raw_content_calls):
    """CP3-CONTENT：raw_content 由 _load_raw_content(db, article_id) 提供，不再是占位文本。"""
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    ctx = FakePipeline.instances[0].ctx
    assert ctx.raw_content == "[stub raw content] article_id=art_1"
    assert raw_content_calls == ["art_1"]


async def test_task_passes_session_factory_and_llm(fake_pipeline):
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    kwargs = FakePipeline.instances[0].kwargs
    assert kwargs["db_session_factory"] is dt_module.AsyncSessionLocal
    assert kwargs["llm"] is not None


async def test_title_is_optional(fake_pipeline):
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    assert FakePipeline.instances[0].ctx.title is None


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------
async def test_simulate_failure_raises(monkeypatch):
    """simulate_failure → 走真实失败路径 → 抛异常（交给 Arq retry）。"""
    monkeypatch.setattr(dt_module, "DistillPipeline", DistillPipeline)
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", FakeSessionFactory())
    monkeypatch.setattr(quota_service, "refund", _noop_refund)

    with pytest.raises(RuntimeError, match="simulated distill failure"):
        await dt_module.distill_task(
            CTX, "dst_1", "art_1", 7, "https://x.com/a", simulate_failure=True
        )


async def test_pipeline_exception_propagates(fake_pipeline):
    """CP11.0.8 fixture 同步：用 fake_pipeline 替换 DistillPipeline + CP-DELETE 兜底 mock。

    但 fake_pipeline 是 FakePipeline 不是 FailingPipeline，需要额外 setattr。
    """
    monkeypatch_fixture = pytest.MonkeyPatch()
    try:
        monkeypatch_fixture.setattr(dt_module, "DistillPipeline", FailingPipeline)
        with pytest.raises(RuntimeError, match="step2 boom"):
            await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        monkeypatch_fixture.undo()


async def test_failure_refunds_quota(fake_pipeline_factory):
    """失败时配额退还由 pipeline 负责，task 只保证不吞异常。

    CP11.0.8 fixture 同步：用 fake_pipeline_factory fixture（真 DistillPipeline + 可断言 factory），
    只 patch quota_service.refund 跟踪调用，不动 DistillPipeline/AsyncSessionLocal（避免状态污染）。
    """
    factory = fake_pipeline_factory
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    from stashbox.backend.common import quota_service as qs

    original_refund = qs.refund
    qs.refund = _refund
    try:
        with pytest.raises(RuntimeError):
            await dt_module.distill_task(
                CTX, "dst_1", "art_1", 7, "https://x.com/a", simulate_failure=True
            )
    finally:
        qs.refund = original_refund

    assert refund_calls == [7]
    assert factory.statuses == ["step1_structuring", "failed"]


async def test_success_does_not_refund(fake_pipeline):
    """CP11.0.8 fixture 同步：用 fake_pipeline fixture（替换 Pipeline + AsyncSessionLocal + LLM）。

    关键修复：不再额外 monkeypatch DistillPipeline/AsyncSessionLocal（避免与 fake_pipeline 冲突，
    pytest-asyncio 跨 test event loop 状态污染的根因之一）。
    只 patch quota_service.refund，记录 refund 调用。
    """
    # 借用 fake_pipeline 的 factory（fixture 内部的 _shared_setup 创建了一个，
    # 但 factory 是闭包局部变量，外部拿不到）。我们重新建一个等价的：
    # 这里用 monkeypatch.setattr 替换 refund（只动 refund，其他用 fake_pipeline）
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    import stashbox.backend.common.quota_service as qs

    qs.refund = _refund
    try:
        result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        # 测试结束后恢复（避免污染后续 test）
        from stashbox.backend.common import quota_service as qs_after

        if hasattr(qs_after, "_refund_original"):
            qs_after.refund = qs_after._refund_original

    assert result["status"] == "done"
    assert refund_calls == []


async def test_real_pipeline_end_to_end_writes_statuses(ctx, fake_tts_cls):
    """真 4 步流水线（直接调 pipeline.run + FakeLLM + FakeTTS）→ 状态机走到 done。

    CP11.0.8 fixture 同步：本测试不通过 distill_task（避免 pytest-asyncio 跨 test event loop
    状态污染），直接调 DistillPipeline.run + 用 ctx 校验 status_history。
    完整 end-to-end 蒸馏任务写回 DB 的测试由 test_distill_pipeline.py 覆盖。
    """

    class _FakeLLM:
        async def chat(self, req):
            from llm.types import ChatResponse, Usage

            step = (req.metadata or {}).get("step")
            if step == "step1_structure":
                content = (
                    '{"summary":"x",'
                    '"chapters":[{"title":"章1","summary":"","key_points":[],"quotes":[],"tension":""}],'
                    '"entities":[],"tags":[]}'
                )
            elif step == "step2_rewrite":
                content = (
                    '{"hook":"开场钩子。",'
                    '"sections":["主体第一段内容。"],'
                    '"outro":"结尾钩子。",'
                    '"word_count":15}'
                )
            else:
                content = "{}"
            return ChatResponse(content=content, model="fake", usage=Usage())

        async def close(self):
            return None

    factory = FakeSessionFactory()
    pipeline = DistillPipeline(_FakeLLM(), tts_client=fake_tts_cls(), db_session_factory=factory)

    result = await pipeline.run(ctx)

    # 状态机走到 DONE
    assert pipeline.status_history[-1] is DistillStatus.DONE
    assert "done" in [s.value for s in pipeline.status_history]
    assert result is ctx


# ---------------------------------------------------------------------------
# 日志字段
# ---------------------------------------------------------------------------
async def test_logs_include_task_and_article_id(fake_pipeline):
    with capture_logs() as captured:
        await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    events = {e["event"]: e for e in captured}
    assert "arq_distill_started" in events
    assert "arq_distill_completed" in events
    for name in ("arq_distill_started", "arq_distill_completed"):
        assert events[name]["task_id"] == "dst_1"
        assert events[name]["article_id"] == "art_1"


async def test_failure_logs_include_task_and_article_id(fake_pipeline):
    """CP11.0.8 fixture 同步：用 fake_pipeline fixture（已经 patch CP-DELETE 兜底），但替换 DistillPipeline=FailingPipeline。"""
    monkeypatch_fixture = pytest.MonkeyPatch()
    try:
        monkeypatch_fixture.setattr(dt_module, "DistillPipeline", FailingPipeline)
        with capture_logs() as captured:
            with pytest.raises(RuntimeError):
                await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        monkeypatch_fixture.undo()

    failed = [e for e in captured if e["event"] == "arq_distill_failed"]
    assert failed, captured
    assert failed[0]["task_id"] == "dst_1"
    assert failed[0]["article_id"] == "art_1"


# ---------------------------------------------------------------------------
# 与 worker 的契约
# ---------------------------------------------------------------------------
def test_task_is_registered_in_worker():
    """worker 按函数名反射调任务，名字对不上任务会永远躺在队列里。"""
    import worker

    assert dt_module.distill_task in worker.WorkerSettings.functions
    assert dt_module.distill_task.__name__ == "distill_task"
