"""DistillPipeline 编排测试（任务包 §4.2）。

用 FakeLLM + MockTTSClient + 假 session factory，不接真 LLM / TTS / DB。
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.sql.elements import BindParameter

from distill import (
    DistillPipeline,
    DistillStatus,
    MockTTSClient,
    step1_structure,
    step2_rewrite,
    step3_tts,
    step4_concat,
)
from distill import pipeline as pipeline_module
from llm import MockLLMClient
from stashbox.backend.common import quota_service


def _unwrap(value):
    """`.values()` 里的字面量会被包成 BindParameter，取值时剥掉一层。"""
    return value.value if isinstance(value, BindParameter) else value


class FakeSession:
    """记录 execute() 收到的语句，不做真实 DB IO。"""

    def __init__(self, recorder: list):
        self.recorder = recorder
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.recorder.append(stmt)
        return None

    async def commit(self):
        self.commits += 1


class FakeSessionFactory:
    def __init__(self):
        self.statements: list = []
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(self.statements)
        self.sessions.append(session)
        return session

    @property
    def values(self) -> list[dict]:
        """每条 UPDATE 语句写入的字段（{列名: 值}）。"""
        return [
            {column.key: _unwrap(value) for column, value in stmt._values.items()}
            for stmt in self.statements
        ]

    @property
    def statuses(self) -> list[str]:
        return [row["status"] for row in self.values if "status" in row]


def _make_pipeline(monkeypatch, llm, *, with_db: bool = True, tts_client=None):
    factory = FakeSessionFactory() if with_db else None
    refund = AsyncMock()
    monkeypatch.setattr(quota_service, "refund", refund)
    pipeline = DistillPipeline(llm, tts_client=tts_client, db_session_factory=factory)
    return pipeline, factory, refund


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------
async def test_happy_path_advances_status_machine(ctx, fake_llm_cls, monkeypatch):
    pipeline, factory, refund = _make_pipeline(monkeypatch, fake_llm_cls('{"summary":"s"}'))

    result = await pipeline.run(ctx)

    assert result is ctx
    assert pipeline.status_history == [
        DistillStatus.QUEUED,
        DistillStatus.STEP1_STRUCTURING,
        DistillStatus.STEP2_REWRITING,
        DistillStatus.STEP3_TTSING,
        DistillStatus.STEP4_CONCATENATING,
        DistillStatus.DONE,
    ]
    refund.assert_not_called()


async def test_happy_path_fills_all_context_stages(ctx, fake_llm_cls, monkeypatch):
    pipeline, _, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    await pipeline.run(ctx)

    assert ctx.structured is not None
    assert ctx.rewrite is not None
    assert ctx.tts is not None
    assert ctx.final is not None
    assert ctx.final.duration_sec == 1800


async def test_happy_path_writes_statuses_to_db(ctx, fake_llm_cls, monkeypatch):
    pipeline, factory, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    await pipeline.run(ctx)

    assert factory.statuses == [
        "step1_structuring",
        "step2_rewriting",
        "step3_ttsing",
        "step4_concatenating",
        "done",
    ]
    assert factory.sessions  # 每次写回都开了一个 session
    assert all(session.commits >= 1 for session in factory.sessions)


async def test_happy_path_writes_final_result_to_db(ctx, fake_llm_cls, monkeypatch):
    pipeline, factory, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    await pipeline.run(ctx)

    final_row = next(row for row in factory.values if "audio_url" in row)
    assert final_row["audio_url"] == ctx.final.audio_url
    assert final_row["duration_sec"] == 1800
    assert final_row["tags"] == ctx.structured.tags
    assert final_row["quality_score"] == 8.5


async def test_pipeline_works_without_session_factory(ctx, fake_llm_cls, monkeypatch):
    """不接 DB（单测 / 未接库场景）也能跑完 4 步。"""
    pipeline, factory, refund = _make_pipeline(
        monkeypatch, fake_llm_cls("改写稿正文"), with_db=False
    )

    await pipeline.run(ctx)

    assert factory is None
    assert pipeline.status_history[-1] is DistillStatus.DONE
    refund.assert_not_called()  # session_factory 为 None 时不退配额


async def test_pipeline_defaults_to_mock_tts_client(ctx, fake_llm_cls):
    pipeline = DistillPipeline(fake_llm_cls("改写稿正文"))

    assert isinstance(pipeline.tts_client, MockTTSClient)


async def test_pipeline_runs_with_real_mock_llm_client(ctx):
    """端到端（mock 依赖）：MockLLMClient + MockTTSClient 跑通 4 步。"""
    llm = MockLLMClient(latency_ms=0)
    pipeline = DistillPipeline(llm)

    await pipeline.run(ctx)

    assert pipeline.status_history[-1] is DistillStatus.DONE
    assert len(ctx.tts.segments) == 2
    await llm.close()


# ---------------------------------------------------------------------------
# 失败路径：任一步抛异常 → FAILED + 退还配额
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "step_name,step_fn,expected_status_at_failure",
    [
        ("step1_structure", step1_structure, DistillStatus.STEP1_STRUCTURING),
        ("step2_rewrite", step2_rewrite, DistillStatus.STEP2_REWRITING),
        ("step3_tts", step3_tts, DistillStatus.STEP3_TTSING),
        ("step4_concat", step4_concat, DistillStatus.STEP4_CONCATENATING),
    ],
)
async def test_failure_at_any_step_fails_and_refunds(
    ctx, fake_llm_cls, monkeypatch, step_name, step_fn, expected_status_at_failure
):
    pipeline, factory, refund = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))
    boom = RuntimeError(f"{step_name} boom")

    async def _boom(*args, **kwargs):
        raise boom

    monkeypatch.setattr(pipeline_module, step_name, _boom)

    with pytest.raises(RuntimeError) as exc:
        await pipeline.run(ctx)

    assert exc.value is boom
    assert pipeline.status_history[-1] is DistillStatus.FAILED
    assert pipeline.status_history[-2] is expected_status_at_failure
    refund.assert_awaited_once()
    assert refund.await_args.args[1] == ctx.user_id


async def test_failure_writes_failed_status_to_db(ctx, fake_llm_cls, monkeypatch):
    pipeline, factory, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "step2_rewrite", _boom)

    with pytest.raises(RuntimeError):
        await pipeline.run(ctx)

    assert factory.statuses == ["step1_structuring", "step2_rewriting", "failed"]


async def test_failure_does_not_write_final_result(ctx, fake_llm_cls, monkeypatch):
    pipeline, factory, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "step4_concat", _boom)

    with pytest.raises(RuntimeError):
        await pipeline.run(ctx)

    assert all("audio_url" not in row for row in factory.values)
    assert ctx.final is None


async def test_failure_without_session_factory_still_raises(ctx, fake_llm_cls, monkeypatch):
    pipeline, _, refund = _make_pipeline(
        monkeypatch, fake_llm_cls("改写稿正文"), with_db=False
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "step1_structure", _boom)

    with pytest.raises(RuntimeError):
        await pipeline.run(ctx)

    assert pipeline.status_history[-1] is DistillStatus.FAILED
    refund.assert_not_called()  # 没有 session 就不退（交给调用方）


async def test_llm_error_propagates_and_refunds(ctx, fake_llm_cls, monkeypatch):
    """LLM 自身抛错（如 RateLimitError）也走同一条失败路径。"""
    llm = fake_llm_cls("改写稿正文", raise_error=RuntimeError("rate limited"))
    pipeline, factory, refund = _make_pipeline(monkeypatch, llm)

    with pytest.raises(RuntimeError, match="rate limited"):
        await pipeline.run(ctx)

    assert pipeline.status_history == [DistillStatus.QUEUED, DistillStatus.STEP1_STRUCTURING, DistillStatus.FAILED]
    refund.assert_awaited_once()


async def test_status_history_resets_between_runs(ctx, fake_llm_cls, monkeypatch):
    pipeline, _, _ = _make_pipeline(monkeypatch, fake_llm_cls("改写稿正文"))

    await pipeline.run(ctx)
    first = list(pipeline.status_history)
    await pipeline.run(ctx)

    assert pipeline.status_history == first  # 不累积，两次 run 互不影响
