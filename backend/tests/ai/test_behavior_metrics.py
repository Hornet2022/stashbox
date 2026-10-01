"""收听行为指标计算（2026-10-02）。

这三个指标此前完全没有实现，导致 user_listening_patterns 的
skip_rate / completion_rate / avg_session_sec 恒为 NULL。
"""

from __future__ import annotations

import pytest


class _Row:
    """模拟 SQLAlchemy Row：既支持属性访问，也支持下标访问。

    生产代码用的是 ``r[0]`` / ``r[1]``（SQLAlchemy Row 的元组接口），
    桩只给属性的话测出来的失败是桩的问题，不是代码的问题。
    """

    def __init__(self, position_sec, total_sec):
        self.position_sec = position_sec
        self.total_sec = total_sec

    def __getitem__(self, idx):
        return (self.position_sec, self.total_sec)[idx]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """最小 AsyncSession 替身：execute 返回预设行集。"""

    def __init__(self, rows):
        self._rows = rows
        self.executed = 0

    async def execute(self, *args, **kwargs):
        self.executed += 1
        return _Result(self._rows)

    async def flush(self):
        return None

    async def scalar(self, *args, **kwargs):
        return None


def _rows(*pairs):
    return [_Row(p, t) for p, t in pairs]


class TestComputeBehaviorMetrics:
    async def test_no_rows_returns_all_none(self):
        from distill.behavior_metrics import compute_behavior_metrics

        m = await compute_behavior_metrics(_Session([]), 1)
        assert m.sample_count == 0
        assert m.skip_rate is None
        assert m.completion_rate is None
        assert m.avg_session_sec is None

    async def test_completion_uses_90_percent_threshold(self):
        """听到 90% 即算听完；80% 不算。"""
        from distill.behavior_metrics import compute_behavior_metrics

        # total=100：90 算完，80 不算
        m = await compute_behavior_metrics(_Session(_rows((95, 100), (80, 100), (100, 100))), 1)
        assert m.completion_rate == pytest.approx(2 / 3, abs=0.01)

    async def test_skip_uses_30s_floor(self):
        """早退判定：低于 max(30s, total*0.2) 记跳过。

        total=100 → 阈值 max(30, 20) = 30，所以 pos=10 算跳过、pos=29 也算。
        """
        from distill.behavior_metrics import compute_behavior_metrics

        m = await compute_behavior_metrics(
            _Session(_rows((10, 100), (29, 100), (50, 100), (60, 100))), 1
        )
        assert m.skip_rate == pytest.approx(0.5, abs=0.01)

    async def test_skip_threshold_scales_with_long_articles(self):
        """长音频不能用 30s 一刀切：total=1800 时阈值是 360s。"""
        from distill.behavior_metrics import compute_behavior_metrics

        rows = _rows((100, 1800), (200, 1800), (400, 1800), (1800, 1800))
        m = await compute_behavior_metrics(_Session(rows), 1)
        assert m.skip_rate == pytest.approx(0.5, abs=0.01)

    async def test_rows_without_total_are_excluded_from_rates(self):
        """没有分母的样本不参与完听/跳过判定，但参与平均时长。"""
        from distill.behavior_metrics import compute_behavior_metrics

        rows = _rows((100, None), (100, 100), (100, 100), (100, 100))
        m = await compute_behavior_metrics(_Session(rows), 1)
        # 4 行都算样本 → 平均时长 = 100
        assert m.avg_session_sec == 100.0
        # 只有 3 行有分母
        assert m.completion_rate == pytest.approx(1.0, abs=0.01)

    async def test_too_few_judged_samples_yields_no_rates(self):
        """有分母的样本 < 3 时不给率 —— 一个样本算出来的"率"没有意义。"""
        from distill.behavior_metrics import compute_behavior_metrics

        m = await compute_behavior_metrics(_Session(_rows((50, 100), (60, 100))), 1)
        assert m.skip_rate is None
        assert m.completion_rate is None
        # 但平均时长还是给
        assert m.avg_session_sec == 55.0

    async def test_avg_session_uses_all_samples(self):
        from distill.behavior_metrics import compute_behavior_metrics

        m = await compute_behavior_metrics(
            _Session(_rows((30, None), (60, 100), (90, 100), (120, 100))), 1
        )
        assert m.avg_session_sec == 75.0

    async def test_query_failure_degrades_gracefully(self):
        """DB 挂了要能降级，不能把蒸馏链路带崩。"""

        class _Broken:
            async def execute(self, *a, **k):
                raise RuntimeError("db down")

        from distill.behavior_metrics import compute_behavior_metrics

        m = await compute_behavior_metrics(_Broken(), 1)
        assert m.sample_count == 0
        assert m.completion_rate is None
