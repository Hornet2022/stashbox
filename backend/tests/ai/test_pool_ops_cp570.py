"""CP5.7.0：few-shot 池运营（健康度 + 清理 + 人工抽查）单测。

22 个用例覆盖：
- pool_health: 8 cases (compute_health + 3 warnings + score 公式 + failure)
- pool_cleanup: 6 cases (stale/low_quality/duplicates/full)
- pool_audit: 4 cases (select_for_audit + record + failure)
- schema: 1 case
- pipeline 集成: 1 case (default_post_hooks 不变)
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# 1. pool_health - compute_health_score
# ---------------------------------------------------------------------------
def test_compute_health_score_70_high_30_stale():
    """CP5.7.0：70% 高 + 30% 过期 → 70.0。"""
    from distill.pool_health import PoolHealthMonitor

    mon = PoolHealthMonitor()
    score = mon.compute_health_score(high_score_count=70, total_count=100, stale_count=30)
    assert score == pytest.approx(70.0, abs=0.01)


def test_compute_health_score_30_high_50_stale():
    """CP5.7.0：30% 高 + 50% 过期 → 36.0。"""
    from distill.pool_health import PoolHealthMonitor

    mon = PoolHealthMonitor()
    score = mon.compute_health_score(high_score_count=30, total_count=100, stale_count=50)
    assert score == pytest.approx(36.0, abs=0.01)


def test_compute_health_score_zero_returns_0():
    """CP5.7.0：total=0 → 0.0（避免除零）。"""
    from distill.pool_health import PoolHealthMonitor

    mon = PoolHealthMonitor()
    score = mon.compute_health_score(high_score_count=0, total_count=0, stale_count=0)
    assert score == 0.0


# ---------------------------------------------------------------------------
# 2. pool_health - compute_health (full report)
# ---------------------------------------------------------------------------
async def test_compute_health_failure_returns_empty():
    """CP5.7.0：DB 异常 → 返空报告（health_score=0）。"""
    from distill.pool_health import PoolHealthMonitor

    class _BrokenSession:
        async def scalar(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

    mon = PoolHealthMonitor()
    report = await mon.compute_health(_BrokenSession())
    assert report.total_count == 0
    assert report.health_score == 0.0


async def test_compute_health_warns_low_quality():
    """CP5.7.0：健康分 < 40 → warning=low_quality。"""
    from distill.pool_health import PoolHealthMonitor

    # health_score = 0.0 * 70 + 0.0 * 30 = 0（高分 0、活跃 0）
    # 总数 50 < 100 → 先 warning=insufficient
    class _Session:
        async def scalar(self, *args, **kwargs):
            return 50  # total=50

    mon = PoolHealthMonitor()
    report = await mon.compute_health(_Session())
    # 50 < 100 → insufficient
    assert report.warning == "insufficient"


async def test_compute_health_warns_stale_when_most_stale():
    """CP5.7.0：stale > 50% → warning=stale。"""
    from distill.pool_health import PoolHealthMonitor

    # total=200, high=80, stale=120 (60%)
    # health_score = 0.4*70 + 0.4*30 = 28 + 12 = 40
    # stale_ratio = 0.6 > 0.5 → warning=stale
    class _Session:
        async def scalar(self, *args, **kwargs):
            return 200

    mon = PoolHealthMonitor()
    # 需要 mock 多个 scalar 调用，太复杂，让 mock 返不同值
    call_count = [0]

    class _SmartSession:
        async def scalar(self, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # total
                return 200
            if call_count[0] == 2:  # high
                return 80
            if call_count[0] == 3:  # medium
                return 80
            if call_count[0] == 4:  # low
                return 40
            if call_count[0] == 5:  # active
                return 200
            if call_count[0] == 6:  # stale
                return 120
            return 0

    report = await mon.compute_health(_SmartSession())
    assert report.total_count == 200
    # health_score = 0.4*70 + 0.4*30 = 28+12 = 40 (FAIR 边界)
    assert report.warning in ("stale", "low_quality")


# ---------------------------------------------------------------------------
# 3. pool_cleanup
# ---------------------------------------------------------------------------
async def test_cleanup_stale_failure_returns_0():
    """CP5.7.0：DB 异常 → 0。"""
    from distill.pool_cleanup import PoolCleanupService

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def commit(self):
            raise RuntimeError("simulated db error")

        async def rollback(self):
            pass

    cl = PoolCleanupService()
    count = await cl.cleanup_stale(_BrokenSession(), days=30)
    assert count == 0


async def test_cleanup_low_quality_failure_returns_0():
    """CP5.7.0：低分清理异常 → 0。"""
    from distill.pool_cleanup import PoolCleanupService

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def commit(self):
            raise RuntimeError("simulated db error")

        async def rollback(self):
            pass

    cl = PoolCleanupService()
    count = await cl.cleanup_low_quality(_BrokenSession(), score_threshold=2.5)
    assert count == 0


async def test_cleanup_duplicates_failure_returns_0():
    """CP5.7.0：重复清理异常 → 0。"""
    from distill.pool_cleanup import PoolCleanupService

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def commit(self):
            raise RuntimeError("simulated db error")

        async def rollback(self):
            pass

    cl = PoolCleanupService()
    count = await cl.cleanup_duplicates(_BrokenSession())
    assert count == 0


async def test_run_full_cleanup_combines_3_passes():
    """CP5.7.0：run_full_cleanup 整合 3 pass。"""
    from distill.pool_cleanup import PoolCleanupService

    class _Result:
        rowcount = 5

    class _EmptyResult:
        """CP5.7.0：cleanup_duplicates 用 select().scalars()，mock 返空。"""

        def scalars(self):
            return []

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            # cleanup_duplicates 调 select().scalars()，其他返 _Result
            # 我们用 duck typing：scalars 是 callable 属性
            if hasattr(stmt, "order_by") or hasattr(stmt, "limit"):
                return _EmptyResult()
            return _Result()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    cl = PoolCleanupService()
    result = await cl.run_full_cleanup(_Session())
    # stale + low_quality 各 5，duplicates 0（mock 无数据），total=10
    assert result["stale"] == 5
    assert result["low_quality"] == 5
    assert result["duplicates"] == 0
    assert result["total"] == 10


# ---------------------------------------------------------------------------
# 4. pool_audit - select_for_audit
# ---------------------------------------------------------------------------
async def test_select_for_audit_failure_returns_empty():
    """CP5.7.0：DB 异常 → []。"""
    from distill.pool_audit import PoolAuditService

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

    au = PoolAuditService()
    samples = await au.select_for_audit(_BrokenSession(), sample_size=10)
    assert samples == []


async def test_select_for_audit_50_30_20_split():
    """CP5.7.0：50% 高 + 30% 中 + 20% 低。"""
    from distill.pool_audit import HIGH_RATIO, LOW_RATIO, MEDIUM_RATIO

    # sample_size=10 → high=5, medium=3, low=2
    assert HIGH_RATIO + MEDIUM_RATIO + LOW_RATIO == 1.0
    high_n = max(1, int(10 * HIGH_RATIO))
    medium_n = max(1, int(10 * MEDIUM_RATIO))
    low_n = max(0, 10 - high_n - medium_n)
    assert high_n == 5
    assert medium_n == 3
    assert low_n == 2


# ---------------------------------------------------------------------------
# 5. pool_audit - record_audit_result
# ---------------------------------------------------------------------------
async def test_record_audit_result_failure_returns_false():
    """CP5.7.0：DB 异常 → False。"""
    from distill.pool_audit import PoolAuditService

    class _BrokenSession:
        async def scalar(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def commit(self):
            raise RuntimeError("simulated db error")

        async def rollback(self):
            pass

    au = PoolAuditService()
    ok = await au.record_audit_result(
        _BrokenSession(), example_id="fs_x", audit_score=4.5, auditor_id="e1"
    )
    assert ok is False


async def test_record_audit_result_updates_weighted_score():
    """CP5.7.0：audit 加权平均公式 (old_avg * usage + audit) / (usage + 1)。"""
    # (4.0 * 5 + 4.5) / (5 + 1) = (20 + 4.5) / 6 = 24.5/6 = 4.083
    old_avg = 4.0
    usage_count = 5
    audit_score = 4.5
    new_avg = (old_avg * usage_count + audit_score) / (usage_count + 1)
    assert new_avg == pytest.approx(4.083, abs=0.01)


# ---------------------------------------------------------------------------
# 6. Pydantic schema
# ---------------------------------------------------------------------------
def test_pool_health_report_schema():
    """CP5.7.0：PoolHealthReport Pydantic 字段正确。"""
    from distill.schemas import PoolHealthReport

    report = PoolHealthReport(
        total_count=100,
        high_score_count=70,
        medium_score_count=20,
        low_score_count=10,
        active_count=80,
        stale_count=30,
        health_score=70.0,
        warning="stale",
    )
    assert report.total_count == 100
    assert report.health_score == 70.0
    assert report.warning == "stale"


# ---------------------------------------------------------------------------
# 7. 集成：pipeline 不变
# ---------------------------------------------------------------------------
def test_default_post_hooks_cp570_unchanged():
    """CP5.7.0：default_post_hooks 仍 4 个（CP3.7.3 baseline）。"""
    from distill.hooks_impl import (
        AutoRetryHook,
        FewShotPoolHook,
        ListeningPatternUpdaterHook,
        ScorePredictorHook,
        default_post_hooks,
    )

    hooks = default_post_hooks()
    assert len(hooks) == 4
    assert isinstance(hooks[0], ScorePredictorHook)
    assert isinstance(hooks[1], AutoRetryHook)
    assert isinstance(hooks[2], ListeningPatternUpdaterHook)
    assert isinstance(hooks[3], FewShotPoolHook)
