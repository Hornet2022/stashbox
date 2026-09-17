"""CP3.6 蒸馏 Prometheus metrics 单测。

验证：
- @trace_distill_step 埋点 attempt / success / failure / duration metrics
- CostTracker.record_llm_usage 埋点 llm_cost_usd_total counter
- distill_queue_size gauge 正常 set
"""
import pytest

from observability.metrics import (
    DISTILL_STEP_DURATION,
    DISTILL_ATTEMPT_TOTAL,
    DISTILL_SUCCESS_TOTAL,
    DISTILL_FAILURE_TOTAL,
    LLM_COST_USD_TOTAL,
    DISTILL_QUEUE_SIZE,
)


@pytest.fixture
def reset_metrics():
    """重置 counter（避免测试间污染）。"""
    yield
    # Counter 不能直接 reset —— 用 _value._value = 0 hack
    for m in [
        DISTILL_ATTEMPT_TOTAL,
        DISTILL_SUCCESS_TOTAL,
        DISTILL_FAILURE_TOTAL,
        LLM_COST_USD_TOTAL,
    ]:
        for child in list(m._metrics.values()):
            child._value.set(0)


async def test_trace_distill_step_increments_metrics(reset_metrics):
    """@trace_distill_step 装饰成功 → attempt/success/duration inc"""
    from observability.decorators import trace_distill_step

    @trace_distill_step("test_step")
    async def fake_step(ctx):
        return "ok"

    ctx = type("C", (), {"task_id": "t", "article_id": "a", "user_id": 1})()
    await fake_step(ctx)

    assert DISTILL_ATTEMPT_TOTAL.labels(step="test_step")._value.get() == 1
    assert DISTILL_SUCCESS_TOTAL.labels(step="test_step")._value.get() == 1
    # duration 至少 > 0
    samples = list(DISTILL_STEP_DURATION.labels(step="test_step")._buckets)
    assert any(s.get() > 0 for s in samples)


async def test_trace_distill_step_failure(reset_metrics):
    """@trace_distill_step 装饰抛异常 → attempt/failure inc, 不 inc success"""
    from observability.decorators import trace_distill_step

    @trace_distill_step("fail_step")
    async def failing_step(ctx):
        raise ValueError("boom")

    ctx = type("C", (), {"task_id": "t", "article_id": "a", "user_id": 1})()
    with pytest.raises(ValueError):
        await failing_step(ctx)

    assert DISTILL_FAILURE_TOTAL.labels(step="fail_step", reason="ValueError")._value.get() == 1
    assert DISTILL_SUCCESS_TOTAL.labels(step="fail_step")._value.get() == 0


async def test_cost_tracker_inc_llm_cost(reset_metrics):
    """CostTracker.record_llm_usage → LLM_COST_USD_TOTAL inc"""
    from observability.cost_tracker import CostTracker

    # Minimal fake redis client (same pattern as test_cost_tracker.py)
    class FakePipeline:
        def __init__(self):
            self.commands = []

        def hincrbyfloat(self, key, field, amount):
            self.commands.append(("hincrbyfloat", key, field, amount))
            return self

        def hincrby(self, key, field, amount):
            self.commands.append(("hincrby", key, field, amount))
            return self

        def expire(self, key, ttl):
            self.commands.append(("expire", key, ttl))
            return self

        async def execute(self):
            return [None] * len(self.commands)

    class FakeRedis:
        def __init__(self):
            self.pipelines = []

        def pipeline(self):
            pipe = FakePipeline()
            self.pipelines.append(pipe)
            return pipe

        async def aclose(self):
            pass

    tracker = CostTracker(client=FakeRedis())
    cost = await tracker.record_llm_usage(
        user_id=1, article_id="a",
        model="claude-4-sonnet-20250514",
        prompt_tokens=1000, completion_tokens=500,
    )
    assert cost > 0
    # 1.0 input @ 0.003 + 0.5 output @ 0.015 = 0.003 + 0.0075 = 0.0105
    assert LLM_COST_USD_TOTAL.labels(model="claude-4-sonnet-20250514")._value.get() == cost


async def test_distill_queue_size_gauge():
    """DISTILL_QUEUE_SIZE.set 正常 inc gauge"""
    DISTILL_QUEUE_SIZE.labels(queue="stashbox:distill").set(42)
    val = DISTILL_QUEUE_SIZE.labels(queue="stashbox:distill")._value.get()
    assert val == 42
    # reset
    DISTILL_QUEUE_SIZE.labels(queue="stashbox:distill")._value.set(0)
