"""Arq worker task 单测（任务包 §4.2）。

两层覆盖：
- 用 FakePipeline 隔离测「参数 → DistillContext」适配 + 返回值 + 异常传播
- 用真 DistillPipeline + 假 session factory 测端到端（失败退配额 / DB 状态写回）
"""
import pytest
from structlog.testing import capture_logs

from tasks import distill_task as dt_module
from distill import DistillPipeline
from stashbox.backend.common import quota_service


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
    monkeypatch.setattr(dt_module, "DistillPipeline", FakePipeline)
    return FakePipeline


class FakeSession:
    def __init__(self, recorder: list):
        self.recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.recorder.append(stmt)
        return None

    async def commit(self):
        return None


class FakeSessionFactory:
    def __init__(self):
        self.statements: list = []

    def __call__(self) -> FakeSession:
        return FakeSession(self.statements)

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


async def test_task_fills_raw_content_placeholder(fake_pipeline):
    """CP3.5 还没接抓取器：raw_content 必须有值（DistillContext 必填），用占位文本。"""
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    ctx = FakePipeline.instances[0].ctx
    assert ctx.raw_content
    assert "https://x.com/a" in ctx.raw_content


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


async def test_pipeline_exception_propagates(monkeypatch):
    monkeypatch.setattr(dt_module, "DistillPipeline", FailingPipeline)

    with pytest.raises(RuntimeError, match="step2 boom"):
        await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")


async def test_failure_refunds_quota(monkeypatch):
    """失败时配额退还由 pipeline 负责，task 只保证不吞异常。"""
    monkeypatch.setattr(dt_module, "DistillPipeline", DistillPipeline)
    factory = FakeSessionFactory()
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(quota_service, "refund", _noop_refund)
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    monkeypatch.setattr(quota_service, "refund", _refund)

    with pytest.raises(RuntimeError):
        await dt_module.distill_task(
            CTX, "dst_1", "art_1", 7, "https://x.com/a", simulate_failure=True
        )

    assert refund_calls == [7]
    assert factory.statuses == ["step1_structuring", "failed"]


async def test_success_does_not_refund(monkeypatch):
    monkeypatch.setattr(dt_module, "DistillPipeline", DistillPipeline)
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", FakeSessionFactory())
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    monkeypatch.setattr(quota_service, "refund", _refund)

    result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    assert result["status"] == "done"
    assert refund_calls == []


async def test_real_pipeline_end_to_end_writes_statuses(monkeypatch):
    """真 4 步流水线（MockLLM + MockTTS）+ 假 session：状态机走到 done。"""
    monkeypatch.setattr(dt_module, "DistillPipeline", DistillPipeline)
    factory = FakeSessionFactory()
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", factory)

    result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    assert result == {"task_id": "dst_1", "status": "done"}
    assert factory.statuses == [
        "step1_structuring",
        "step2_rewriting",
        "step3_ttsing",
        "step4_concatenating",
        "done",
    ]


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


async def test_failure_logs_include_task_and_article_id(monkeypatch):
    monkeypatch.setattr(dt_module, "DistillPipeline", FailingPipeline)

    with capture_logs() as captured:
        with pytest.raises(RuntimeError):
            await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

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
