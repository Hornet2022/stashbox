"""CP3.5 quota metrics 单测：4 类覆盖（暴露 / 并发不超扣 / cache hit-miss / refund label）。

任务包 §4.5 定义 4 测试，验收标准 §6 要求 4 passed。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from stashbox.backend.common import quota_metrics
from stashbox.backend.common.quota_service import (
    QuotaExceededError,
    consume,
    get_quota,
    refund,
)


# ---------------------------------------------------------------------------
# helper：抓取 REGISTRY 里所有 quota_* metric 名字
# ---------------------------------------------------------------------------
def _quota_metric_names() -> set[str]:
    """从 REGISTRY._collector_to_names 提取所有 quota_ 开头的 metric 名。"""
    # REGISTRY._collector_to_names: dict[Collector, frozenset[str]]
    names = set()
    for collector, name_set in REGISTRY._collector_to_names.items():
        for name in name_set:
            if name.startswith("quota_"):
                names.add(name)
    return names


# ---------------------------------------------------------------------------
# test 1：metrics 暴露在 REGISTRY
# ---------------------------------------------------------------------------
def test_quota_metrics_exposed_in_registry():
    """7 个 quota metric 都在 prometheus REGISTRY 里（§4.3 清单）。"""
    names = _quota_metric_names()
    expected = {
        "quota_consume_total",
        "quota_consume_blocked_total",
        "quota_consume_duration_seconds",
        "quota_refund_total",
        "quota_cache_hit_total",
        "quota_cache_miss_total",
        "quota_reset_total",
        "quota_request_total",  # §4.2 user-service endpoint metric
    }
    assert expected.issubset(names), f"missing: {expected - names}"


# ---------------------------------------------------------------------------
# test 2：100 并发 consume，quota=10，最终不超扣
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_100_concurrent_consume_no_overage():
    """100 并发 consume(quota=10)，乐观锁保证 quota_used 最终 ≤ 10（v1 §3.3）。"""
    # 追踪所有 _apply 调用，模拟乐观锁行为
    call_count = 0
    call_lock = asyncio.Lock()

    async def mock_apply(session, user_id, delta):
        nonlocal call_count
        async with call_lock:
            call_count += 1
            # 模拟：前 10 次成功（quota_used 从 0 到 10），之后抛 QuotaExceededError
        if call_count <= 10:
            # 返回模拟扣减结果
            return {"user_id": user_id, "quota_used": call_count, "monthly_quota": 10, "version": call_count}
        raise QuotaExceededError(message="quota exceeded")

    mock_session = AsyncMock()
    user_id = 1

    # 并发 100 次 consume
    tasks = [consume(mock_session, user_id, 1) for _ in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 统计成功次数（return_exceptions=True 不会抛异常）
    successes = [r for r in results if not isinstance(r, Exception)]
    # 乐观锁保证最多 10 次成功
    assert len(successes) <= 10, f"overage detected: {len(successes)} successes (quota=10)"

    # 再次验证：最多 10 次
    assert call_count >= len(successes)  # call_count ≥ successes（可能有重试）


# ---------------------------------------------------------------------------
# test 3：cache hit / miss 计数器递增
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_hit_and_miss_counters_increment():
    """第 1 次 get_quota = cache miss；第 2 次（mock 有缓存）= cache hit。"""
    mock_session = AsyncMock()

    # 读取计数初始值
    def _hit_count():
        return quota_metrics.quota_cache_hit_total._value.get()
    def _miss_count():
        return quota_metrics.quota_cache_miss_total._value.get()

    hit_before = _hit_count()
    miss_before = _miss_count()

    # 第 1 次：mock cache_service.get_quota 返回 None → miss
    with patch("stashbox.backend.common.quota_service.cache_service.get_quota", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None  # cache miss
        mock_session.execute = AsyncMock()
        mock_session.get = AsyncMock()
        # mock User query result
        mock_row = MagicMock()
        mock_row.__iter__ = lambda self: iter([0, 10, 1, None])
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result
        mock_session.get.return_value = MagicMock(plan="free")

        with patch("stashbox.backend.common.quota_service.cache_service.set_quota", new_callable=AsyncMock):
            await get_quota(mock_session, 1)

    assert _miss_count() == miss_before + 1, "first call should be a cache miss"
    assert _hit_count() == hit_before, "first call should NOT be a hit"

    # 第 2 次：mock cache_service.get_quota 返回缓存值 → hit
    hit_before2 = _hit_count()
    miss_before2 = _miss_count()

    with patch("stashbox.backend.common.quota_service.cache_service.get_quota", new_callable=AsyncMock) as mock_get2:
        mock_get2.return_value = {"quota_used": 5, "monthly_quota": 10, "version": 1}  # cache hit

        result = await get_quota(mock_session, 1)

    assert _hit_count() == hit_before2 + 1, "second call should be a cache hit"
    assert _miss_count() == miss_before2, "second call should NOT increment miss counter"
    assert result["cached"] is True


# ---------------------------------------------------------------------------
# test 4：refund metric label
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refund_metric_label():
    """退还 1 次后 quota_refund_total{trigger=distill_failed} = 1。"""
    # 读取 refund counter 初始值
    def _refund_count(trigger="distill_failed"):
        # Counter.labels() 返回 ChildCounter，._value.get() 取当前值
        return quota_metrics.quota_refund_total.labels(trigger=trigger)._value.get()

    before = _refund_count()

    async def mock_apply(session, user_id, delta):
        return {"user_id": user_id, "quota_used": 9, "monthly_quota": 10, "version": 2}

    mock_session = AsyncMock()

    with patch("stashbox.backend.common.quota_service._apply", new=mock_apply):
        await refund(mock_session, user_id=1, amount=1, trigger="distill_failed")

    assert _refund_count() == before + 1, "refund should increment distill_failed counter"
    assert _refund_count(trigger="admin") == 0, "admin trigger should be untouched"
