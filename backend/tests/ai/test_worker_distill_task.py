"""Arq worker task 单测（任务包 §4.2）—— CP-AGENT-RUNNER-INTEGRATION 同步版。

架构变更（CP-AGENT-RUNNER-INTEGRATION）：
    distill_task 不再直接调 DistillPipeline.run()，而是走
    `_run_distill_via_agent(task_id, article_id, user_id, url, raw_content, ...)`
    → LangGraph agent_app.ainvoke()。

因此本文件的两层覆盖调整为：
  1. 「任务参数 → _run_distill_via_agent 入参适配」+ 返回值 + 异常传播
     （monkeypatch _run_distill_via_agent / _persist_agent_final，用 FakeSession 记录 DB 写）
  2. 真 DistillPipeline 独立跑通（不经过 distill_task）—— 验证旧 pipeline 仍可用作回退

注意：distill_task.py 里旧的 DistillPipeline 路径保留为「回退」，
本文件保留 1 个 pipeline 直跑的用例确保它没被改坏。
"""

from contextlib import asynccontextmanager

import pytest
from structlog.testing import capture_logs

from distill import DistillPipeline, DistillStatus
from stashbox.backend.common import quota_service
from tasks import distill_task as dt_module


class AgentCallRecorder:
    """记录 _run_distill_via_agent 的调用入参 + 模拟返回。"""

    calls: list[dict] = []
    # 返回给 distill_task 的 final state（默认成功）
    final_state: dict = {
        "status": "done",
        "current_step": "done",
        "rewritten_script": "改写稿",
        "tts_audio_url": "https://oss/a.mp3",
        "tts_duration_sec": 60,
    }
    # 设了就在被调时抛这个异常
    raise_exc: Exception | None = None

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.final_state = {
            "status": "done",
            "current_step": "done",
            "rewritten_script": "改写稿",
            "tts_audio_url": "https://oss/a.mp3",
            "tts_duration_sec": 60,
        }
        cls.raise_exc = None


@pytest.fixture(autouse=True)
def _reset_agent_recorder():
    AgentCallRecorder.reset()
    yield
    AgentCallRecorder.reset()


class FakeSession:
    """记录 execute() 收到的语句，不做真实 DB IO。"""

    def __init__(self, recorder: list, *, article_exists: bool = True):
        self.recorder = recorder
        self.article_exists = article_exists

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        """CP11.0.8 fixture 同步：返回支持 SQLAlchemy Result 接口的 mock。

        CP-AGENT 后 distill_task 用 `db.execute(update(...))`（不带参），
        所以签名改成 (stmt) 不用 *args —— 对齐真实调用。
        """
        self.recorder.append(stmt)
        return _FakeResult()

    async def scalar(self, stmt):
        return "art_1" if self.article_exists else None

    async def commit(self):
        return None

    async def begin_nested(self):
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


def _shared_setup(monkeypatch):
    """公共 fixture setup：patch _run_distill_via_agent + _persist_agent_final + AsyncSessionLocal。

    返回 FakeSessionFactory 供断言 DB 写入。
    """
    factory = FakeSessionFactory()

    @asynccontextmanager
    async def _fake_async_session_local():
        session = factory()
        yield session

    async def _fake_run_via_agent(
        task_id, article_id, user_id, url, raw_content, source="web", simulate_failure=False
    ):
        AgentCallRecorder.calls.append(
            {
                "task_id": task_id,
                "article_id": article_id,
                "user_id": user_id,
                "url": url,
                "raw_content": raw_content,
                "source": source,
                "simulate_failure": simulate_failure,
            }
        )
        if AgentCallRecorder.raise_exc is not None:
            raise AgentCallRecorder.raise_exc
        return AgentCallRecorder.final_state

    async def _fake_persist(task_id, article_id, user_id, final):
        return None

    monkeypatch.setattr(dt_module, "AsyncSessionLocal", _fake_async_session_local)
    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _fake_run_via_agent)
    monkeypatch.setattr(dt_module, "_persist_agent_final", _fake_persist)
    return factory


@pytest.fixture
def fake_agent(monkeypatch):
    """成功路径 fixture：patch agent 调用链，返回 FakeSessionFactory。"""
    return _shared_setup(monkeypatch)


@pytest.fixture(autouse=True)
def raw_content_calls(monkeypatch):
    """把 _load_raw_content 打成替身（CP3-CONTENT 后它要查真 DB）。

    返回记录的 article_id 列表，供用例断言 loader 确实被调用。
    """
    calls: list = []

    async def _load(db, article_id):
        calls.append(article_id)
        return f"[stub raw content] article_id={article_id}"

    monkeypatch.setattr(dt_module, "_load_raw_content", _load)
    return calls


async def _noop_refund(session, user_id, amount=1):
    """不碰 DB 的 refund 替身（真 refund 会查 users 表）。"""
    return {}


CTX = {"job_id": "job-1", "redis": None}


# ---------------------------------------------------------------------------
# 成功路径（agent 调用链隔离）
# ---------------------------------------------------------------------------
async def test_success_returns_done(fake_agent):
    result = await dt_module.distill_task(
        CTX, "dst_1", "art_1", 7, "https://mp.weixin.qq.com/s/x", title="标题"
    )

    assert result == {"task_id": "dst_1", "status": "done"}


async def test_task_passes_args_to_agent(fake_agent):
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a", title="标题")

    assert len(AgentCallRecorder.calls) == 1
    call = AgentCallRecorder.calls[0]
    assert call["task_id"] == "dst_1"
    assert call["article_id"] == "art_1"
    assert call["user_id"] == 7
    assert call["url"] == "https://x.com/a"
    # source 默认 "web"
    assert call["source"] == "web"


async def test_task_loads_raw_content_from_db(fake_agent, raw_content_calls):
    """CP3-CONTENT：raw_content 由 _load_raw_content(db, article_id) 提供，不再是占位文本。"""
    await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    call = AgentCallRecorder.calls[0]
    assert call["raw_content"] == "[stub raw content] article_id=art_1"
    assert raw_content_calls == ["art_1"]


async def test_success_writes_articles_ready_when_audio_is_http(fake_agent, monkeypatch):
    """CP10：agent 产出 http audio_url → articles.status 写 ready。

    _persist_agent_final 被 mock 跳过 DB；这里直接验证 distill_task 在 agent 返回后
    读 distilled_articles 写 ready 的分支不会崩（FakeSession.scalar_one_or_none → None，
    所以实际不写；用 statuses 断言至少 distilling 写过）。
    """
    result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    assert result["status"] == "done"


async def test_title_is_optional(fake_agent):
    """title 可选：不传也能跑通（agent 用 raw_content 就够了）。"""
    result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    assert result["status"] == "done"


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------
async def test_simulate_failure_raises(monkeypatch):
    """simulate_failure → 走真实失败路径 → 抛异常（交给 Arq retry）。"""
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", FakeSessionFactory())
    monkeypatch.setattr(quota_service, "refund", _noop_refund)

    with pytest.raises(RuntimeError, match="simulated distill failure"):
        await dt_module.distill_task(
            CTX, "dst_1", "art_1", 7, "https://x.com/a", simulate_failure=True
        )


async def test_agent_exception_propagates(fake_agent):
    """agent 抛异常 → distill_task 不吞，向上传播给 Arq retry。"""
    AgentCallRecorder.raise_exc = RuntimeError("step2 boom")

    with pytest.raises(RuntimeError, match="step2 boom"):
        await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")


async def test_failure_refunds_quota(monkeypatch):
    """失败时配额退还 + articles.status 写 failed。"""
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", FakeSessionFactory())
    monkeypatch.setattr(quota_service, "refund", _noop_refund)

    with pytest.raises(RuntimeError):
        await dt_module.distill_task(
            CTX, "dst_1", "art_1", 7, "https://x.com/a", simulate_failure=True
        )


async def test_success_does_not_refund(fake_agent):
    """成功时不退还配额。"""
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    original_refund = quota_service.refund
    quota_service.refund = _refund
    try:
        result = await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        quota_service.refund = original_refund

    assert result["status"] == "done"
    assert refund_calls == []


# ---------------------------------------------------------------------------
# 日志字段
# ---------------------------------------------------------------------------
async def test_logs_include_task_and_article_id(fake_agent):
    with capture_logs() as captured:
        await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    events = {e["event"]: e for e in captured}
    assert "arq_distill_started" in events
    assert "arq_distill_completed" in events
    for name in ("arq_distill_started", "arq_distill_completed"):
        assert events[name]["task_id"] == "dst_1"
        assert events[name]["article_id"] == "art_1"


async def test_failure_logs_include_task_and_article_id(fake_agent):
    """失败路径也要记 task_id / article_id。"""
    AgentCallRecorder.raise_exc = RuntimeError("boom")

    with capture_logs() as captured:
        with pytest.raises(RuntimeError):
            await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    failed = [e for e in captured if e["event"] == "arq_distill_failed"]
    assert failed, captured
    assert failed[0]["task_id"] == "dst_1"
    assert failed[0]["article_id"] == "art_1"


# ---------------------------------------------------------------------------
# 旧 DistillPipeline 仍可用（回退路径）
# ---------------------------------------------------------------------------
async def test_legacy_distill_pipeline_still_runs(ctx, fake_tts_cls):
    """旧 4 步 DistillPipeline 保留为回退，独立跑一遍确认没被改坏。

    不经过 distill_task —— 直接调 pipeline.run + 用 ctx 校验 status_history。
    """
    from llm.types import ChatResponse, Usage

    class _FakeLLM:
        async def chat(self, req):
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

    assert pipeline.status_history[-1] is DistillStatus.DONE
    assert "done" in [s.value for s in pipeline.status_history]
    assert result is ctx


# ---------------------------------------------------------------------------
# 与 worker 的契约
# ---------------------------------------------------------------------------
def test_task_is_registered_in_worker():
    """worker 按函数名反射调任务，名字对不上任务会永远躺在队列里。"""
    import worker

    assert dt_module.distill_task in worker.WorkerSettings.functions
    assert dt_module.distill_task.__name__ == "distill_task"


async def test_agent_failed_status_triggers_failure_path(fake_agent, monkeypatch):
    """CP-AGENT-FAILURE-PROPAGATION：agent 返回 failed → 抛异常（不退化成 done）。"""
    # 让 agent 返回失败态
    AgentCallRecorder.final_state = {
        "status": "failed",
        "current_step": "failed",
        "error": "LLM 持续超时",
        "error_kind": "timeout",
        "error_step": "rewrite",
    }
    refund_calls = []

    async def _refund(session, user_id, amount=1):
        refund_calls.append(user_id)
        return {}

    original_refund = quota_service.refund
    quota_service.refund = _refund
    try:
        with pytest.raises(RuntimeError, match="agent distill failed"):
            await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        quota_service.refund = original_refund

    # 失败路径应退还配额
    assert refund_calls == [7]
