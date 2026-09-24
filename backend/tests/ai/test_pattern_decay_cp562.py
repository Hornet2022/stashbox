"""CP5.6.2：画像权重衰减 + 重新聚类单测。

15+ 个用例覆盖：
- pattern_decay: 4 档权重（today/30d/60d/90d）+ future + weighted_avg + apply
- pattern_clustering: 3 桶分桶 + dominant_cluster + 平局 + empty
- 集成：decay + cluster workflow
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# 1. pattern_decay - compute_weight
# ---------------------------------------------------------------------------
def test_compute_weight_today_returns_1():
    """CP5.6.2：今天反馈 → weight=1.0。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    weight = calc.compute_weight(now, now=now)
    # 浮点可能 0.9999999999...
    assert weight == pytest.approx(1.0, abs=0.01)


def test_compute_weight_30_days_returns_05():
    """CP5.6.2：30 天前 → weight=0.5（半衰期）。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    weight = calc.compute_weight(now - timedelta(days=30), now=now)
    assert weight == pytest.approx(0.5, abs=0.01)


def test_compute_weight_60_days_returns_025():
    """CP5.6.2：60 天前 → weight=0.25（半衰 × 2）。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    weight = calc.compute_weight(now - timedelta(days=60), now=now)
    assert weight == pytest.approx(0.25, abs=0.01)


def test_compute_weight_90_days_returns_0125():
    """CP5.6.2：90 天前 → weight=0.125。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    weight = calc.compute_weight(now - timedelta(days=90), now=now)
    assert weight == pytest.approx(0.125, abs=0.01)


def test_compute_weight_negative_days_returns_1():
    """CP5.6.2：未来时间 → weight=1.0（视为最新）。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    weight = calc.compute_weight(now + timedelta(days=1), now=now)
    assert weight == 1.0


def test_compute_weight_none_returns_1():
    """CP5.6.2：feedback_at=None → weight=1.0。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    weight = calc.compute_weight(None)
    assert weight == 1.0


# ---------------------------------------------------------------------------
# 2. pattern_decay - compute_weighted_avg
# ---------------------------------------------------------------------------
def test_compute_weighted_avg_uniform_returns_same():
    """CP5.6.2：均匀时间反馈 → 等同算术平均。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    avg = calc.compute_weighted_avg(
        [
            (4.0, now),
            (5.0, now),
            (3.0, now),
        ],
        now=now,
    )
    # 全 1.0 权重 → (4+5+3)/3 = 4.0
    assert avg == pytest.approx(4.0, abs=0.01)


def test_compute_weighted_avg_with_decay():
    """CP5.6.2：4 today + 4 30d ago → (4*1+4*0.5)/1.5 = 4.0。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    avg = calc.compute_weighted_avg(
        [
            (4.0, now),
            (4.0, now - timedelta(days=30)),
        ],
        now=now,
    )
    assert avg == pytest.approx(4.0, abs=0.01)


def test_compute_weighted_avg_old_low_score_decreases():
    """CP5.6.2：4 today + 2 60d ago → (4*1+2*0.25)/1.25 = 3.6。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    now = datetime.now()
    avg = calc.compute_weighted_avg(
        [
            (4.0, now),
            (2.0, now - timedelta(days=60)),
        ],
        now=now,
    )
    assert avg == pytest.approx(3.6, abs=0.01)


def test_compute_weighted_avg_empty_returns_0():
    """CP5.6.2：空列表 → 0.0。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    avg = calc.compute_weighted_avg([], now=datetime.now())
    assert avg == 0.0


# ---------------------------------------------------------------------------
# 3. pattern_decay - apply_decay_to_pattern
# ---------------------------------------------------------------------------
async def test_apply_decay_to_pattern_empty_returns_none():
    """CP5.6.2：空 evaluations → None。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    result = await calc.apply_decay_to_pattern(db=None, user_id=1, evaluations=[])
    assert result is None


async def test_apply_decay_to_pattern_no_db_returns_none():
    """CP5.6.2：无 DB → None（异常兜底）。"""
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    result = await calc.apply_decay_to_pattern(db=None, user_id=1, evaluations=None)
    assert result is None


# ---------------------------------------------------------------------------
# 4. pattern_clustering - 3 桶分桶
# ---------------------------------------------------------------------------
def test_cluster_3_buckets():
    """CP5.6.2：3 桶分桶（< 3 / 3-4 / >= 4）。"""
    from distill.pattern_clustering import (
        CLUSTER_AGGRESSIVE,
        CLUSTER_CONSERVATIVE,
        CLUSTER_NEUTRAL,
        PatternClustering,
    )

    class _StubEv:
        def __init__(self, score):
            self.overall_score = score

    cl = PatternClustering()
    result = cl.cluster_evaluations(
        [
            _StubEv(1),  # conservative
            _StubEv(2.5),  # conservative
            _StubEv(3),  # neutral
            _StubEv(3.5),  # neutral
            _StubEv(4),  # aggressive
            _StubEv(5),  # aggressive
        ]
    )
    assert len(result[CLUSTER_CONSERVATIVE]) == 2
    assert len(result[CLUSTER_NEUTRAL]) == 2
    assert len(result[CLUSTER_AGGRESSIVE]) == 2


def test_cluster_low_score_conservative():
    """CP5.6.2：评分 < 3 → conservative。"""
    from distill.pattern_clustering import (
        CLUSTER_CONSERVATIVE,
        PatternClustering,
    )

    class _StubEv:
        def __init__(self, score):
            self.overall_score = score

    cl = PatternClustering()
    result = cl.cluster_evaluations([_StubEv(1.5), _StubEv(2.0)])
    assert len(result[CLUSTER_CONSERVATIVE]) == 2
    assert all(len(v) == 0 for k, v in result.items() if k != CLUSTER_CONSERVATIVE)


def test_cluster_high_score_aggressive():
    """CP5.6.2：评分 >= 4 → aggressive。"""
    from distill.pattern_clustering import (
        CLUSTER_AGGRESSIVE,
        PatternClustering,
    )

    class _StubEv:
        def __init__(self, score):
            self.overall_score = score

    cl = PatternClustering()
    result = cl.cluster_evaluations([_StubEv(4.5), _StubEv(5.0)])
    assert len(result[CLUSTER_AGGRESSIVE]) == 2


def test_cluster_none_score_to_neutral():
    """CP5.6.2：score=None → neutral。"""
    from distill.pattern_clustering import (
        CLUSTER_NEUTRAL,
        PatternClustering,
    )

    class _StubEv:
        overall_score = None

    cl = PatternClustering()
    result = cl.cluster_evaluations([_StubEv()])
    assert len(result[CLUSTER_NEUTRAL]) == 1


# ---------------------------------------------------------------------------
# 5. pattern_clustering - dominant_cluster
# ---------------------------------------------------------------------------
def test_dominant_cluster_returns_majority():
    """CP5.6.2：主导 cluster = 数量最多的。"""
    from distill.pattern_clustering import (
        CLUSTER_AGGRESSIVE,
        PatternClustering,
    )

    class _StubEv:
        def __init__(self, score):
            self.overall_score = score

    cl = PatternClustering()
    # 3 个 aggressive + 1 个 conservative + 1 个 neutral
    clustered = cl.cluster_evaluations(
        [_StubEv(4.5), _StubEv(4.5), _StubEv(4.5), _StubEv(1.0), _StubEv(3.5)]
    )
    assert cl.dominant_cluster(clustered) == CLUSTER_AGGRESSIVE


def test_dominant_cluster_tie_returns_conservative():
    """CP5.6.2：平局时按 conservative → neutral → aggressive 优先级。"""
    from distill.pattern_clustering import (
        CLUSTER_CONSERVATIVE,
        PatternClustering,
    )

    class _StubEv:
        def __init__(self, score):
            self.overall_score = score

    cl = PatternClustering()
    # 1 个 conservative + 1 个 neutral + 1 个 aggressive（平局）
    clustered = cl.cluster_evaluations([_StubEv(1.5), _StubEv(3.5), _StubEv(4.5)])
    assert cl.dominant_cluster(clustered) == CLUSTER_CONSERVATIVE


def test_dominant_cluster_empty_returns_neutral():
    """CP5.6.2：空聚类 → neutral default。"""
    from distill.pattern_clustering import CLUSTER_NEUTRAL, PatternClustering

    cl = PatternClustering()
    assert cl.dominant_cluster({}) == CLUSTER_NEUTRAL
    assert (
        cl.dominant_cluster({"conservative": [], "neutral": [], "aggressive": []})
        == CLUSTER_NEUTRAL
    )


# ---------------------------------------------------------------------------
# 6. 集成：decay + cluster workflow
# ---------------------------------------------------------------------------
def test_decay_then_cluster_workflow():
    """CP5.6.2：先 decay → 后 cluster（端到端）。"""
    from distill.pattern_clustering import (
        CLUSTER_AGGRESSIVE,
        CLUSTER_NEUTRAL,
        PatternClustering,
    )
    from distill.pattern_decay import PatternDecayCalculator

    calc = PatternDecayCalculator()
    cl = PatternClustering()

    now = datetime.now()
    # 4 today + 2 60d ago = (4*1 + 2*0.25)/1.25 = 3.6 (衰减后从 4 变成 3.6)
    raw_feedback = [(4.0, now), (2.0, now - timedelta(days=60))]
    weighted_avg = calc.compute_weighted_avg(raw_feedback, now=now)
    assert weighted_avg == pytest.approx(3.6, abs=0.01)

    # 衰减后 3.6 → 聚类到 neutral（< 4.0）
    class _StubEv:
        overall_score = weighted_avg

    clustered = cl.cluster_evaluations([_StubEv()])
    assert len(clustered[CLUSTER_NEUTRAL]) == 1
    assert len(clustered[CLUSTER_AGGRESSIVE]) == 0


# ---------------------------------------------------------------------------
# 7. 集成：pipeline 不变
# ---------------------------------------------------------------------------
def test_default_post_hooks_cp562_unchanged():
    """CP5.6.2：default_post_hooks 仍 4 个（CP3.7.3 baseline）。"""
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
