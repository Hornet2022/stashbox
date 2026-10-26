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
        task_id,
        article_id,
        user_id,
        url,
        raw_content,
        source="web",
        title=None,
        simulate_failure=False,
    ):
        AgentCallRecorder.calls.append(
            {
                "task_id": task_id,
                "article_id": article_id,
                "user_id": user_id,
                "url": url,
                "raw_content": raw_content,
                "source": source,
                # CP-TITLE-ANNOUNCE（2026-10-26）：标题进 agent（给音频开头播报用）
                "title": title,
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


@pytest.fixture(autouse=True)
def material_source_calls(monkeypatch):
    """把 _load_material_source 打成替身（它要查真 DB）。

    返回记录的 article_id 列表，供用例断言确实去查了真实来源。
    """
    calls: list = []

    async def _source(db, article_id):
        calls.append(article_id)
        return "wechat_mp"

    monkeypatch.setattr(dt_module, "_load_material_source", _source)
    return calls


async def _noop_refund(session, user_id, amount=1):
    """不碰 DB 的 refund 替身（真 refund 会查 users 表）。"""
    return {}


CTX = {"job_id": "job-1", "redis": None}
# Arq 的 ctx 带 job_try（1=首次执行，2/3=第 1/2 次重试）。
# max_tries = ARQ_RETRY_MAX + 1 = 3 → job_try=3 是最后一次尝试。
CTX_LAST = {"job_id": "job-1", "redis": None, "job_try": 3}
CTX_RETRY = {"job_id": "job-1", "redis": None, "job_try": 1}


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
    # source 是**真实来源**（从 raw_content.source 查出来的），不是硬编码的 "web"。
    # 它决定改写 prompt 走哪种素材口径（文章 / 文档 / 代码库）——
    # 传错的后果是「用公众号的写法改文档」= 逼出编造。
    assert call["source"] == "wechat_mp"


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


async def test_title_reaches_the_agent(fake_agent):
    """CP-TITLE-ANNOUNCE（2026-10-26）：标题必须进 agent。

    不进 agent ⇒ 音频开头就没有标题播报（用户内测反馈的那条）。
    """
    await dt_module.distill_task(
        CTX, "dst_1", "art_1", 7, "https://x.com/a", title="为什么需要 Ontology"
    )

    call = AgentCallRecorder.calls[0]
    assert call["title"] == "为什么需要 Ontology"


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
    """CP-AGENT-FAILURE-PROPAGATION：agent 返回 failed → 抛异常（不退化成 done）。

    **最后一次**尝试失败才退还配额（Arq 还会重试时不退 —— 见下面那个用例）。
    """
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
            await dt_module.distill_task(CTX_LAST, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        quota_service.refund = original_refund

    # 最后一次尝试失败 → 退还配额
    assert refund_calls == [7]


async def test_intermediate_failure_keeps_distilling_and_defers_refund(fake_agent, monkeypatch):
    """**还有下一次尝试时**：状态保持 distilling，且**不**退配额。

    2026-10-21 内测实测踩到：一篇稿子 LLM 返回空 content → 首轮失败 →
    旧实现无条件把 articles.status 标成 failed → 用户在 Arq 重试的
    15~45 秒窗口里看到「失败 / 换个源试试？点击重新蒸馏」，而那次重试
    **其实成功了**（1052 字稿 / 250 秒音频 / 语速 4.21 正常）。

    代价是实打实的：用户以为彻底失败 → 点「重试蒸馏」→ 排重复任务 →
    白烧一次 LLM+TTS 费用。

    配额同理：中间失败就退（SETNX 只退一次），重试成功的话用户白赚一次。
    """
    AgentCallRecorder.final_state = {
        "status": "failed",
        "current_step": "failed",
        "error": "LLM 返回空内容",
        "error_kind": "internal",
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
            await dt_module.distill_task(CTX_RETRY, "dst_1", "art_1", 7, "https://x.com/a")
    finally:
        quota_service.refund = original_refund

    # 还有下一次尝试 → 不退款
    assert refund_calls == [], "中间失败就退款，重试成功的话用户白赚一次配额"


# ---------------------------------------------------------------------------
# CP-ARQ-RETRY：失败必须抛 arq.Retry，普通异常在 arq 0.28 下不会被重试
# ---------------------------------------------------------------------------
async def test_failure_raises_arq_retry_so_queue_actually_retries(fake_agent):
    """失败路径必须抛 arq.Retry。

    arq 0.28 的 worker 只对 `arq.Retry` / `CancelledError` / `RetryJob` 重试，
    普通异常一律落进 else 分支判 failed（finish=True，不再入队）。
    旧实现抛的是裸 RuntimeError —— 于是 `ARQ_RETRY_MAX=2` 这套配置
    **一次都没生效过**（实测长稿 TTS 超时，job 只执行了 1 次）。

    实测（2026-10-21）：一篇 1047 字的稿子因 TTS 超时失败，
    队列里没有第二次 arq_distill_started，用户只能自己点重试。
    """
    from arq import Retry as ArqRetry

    AgentCallRecorder.raise_exc = RuntimeError("step2 boom")

    with pytest.raises(ArqRetry) as ei:
        await dt_module.distill_task(CTX, "dst_1", "art_1", 7, "https://x.com/a")

    # 原始失败原因必须留在 str() 里 —— arq 的结构化日志只打 str(exc)，
    # 原因丢了就只能去翻 __cause__，排障时等于没有线索。
    assert "step2 boom" in str(ei.value)
    # defer=15s：上游（vLLM）的问题有时间聚集性，立刻重试多半还在坏窗口里
    assert ei.value.defer_score == 15000


async def test_distill_retry_keeps_original_message():
    """DistillRetry 是 arq.Retry 的子类，但 str() 给回原始原因。

    arq 0.28 的 `Retry.__init__` 不接受 message、`__str__` 固定返回
    '<Retry defer 15.00s>' —— 直接抛它，真实失败原因会在 arq 日志里消失。
    """
    from arq import Retry as ArqRetry

    from tasks.distill_task import DistillRetry

    r = DistillRetry("tts_synthesize 失败: OpenAITTS 超时", defer=15)
    assert isinstance(r, ArqRetry)
    assert isinstance(r, RuntimeError)
    assert str(r) == "tts_synthesize 失败: OpenAITTS 超时"
