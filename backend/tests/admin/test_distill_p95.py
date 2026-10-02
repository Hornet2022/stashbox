"""GET /api/v1/admin/distill-p95 的分位数解析测试。

这个函数此前**零测试覆盖**，于是一个纯粹的算术 bug 静默存活了很久：
`+Inf` 桶被折成 `1e18` 参与线性插值，管理后台「蒸馏耗时」页长期显示
`400000000000000320.00 秒`（P50）和 `100000000000000080.00 秒`（整体）。
而同一页的 step1/step2/step4 是对的 —— 所以肉眼扫一眼完全发现不了。

夹具用的是本机真实抓到的 8104 /metrics 片段（step3_tts 6 次采样、
sum=4469.13s，桶上界当时只到 600s，5/6 落进 +Inf），不是手编的理想数据。

覆盖：
  - 正常量程内的分位数（step1/step2/step4）
  - 超量程时返回 None 而不是假数字（step3_tts / overall）
  - count / mean / upper_bound 三个新字段的真实性
  - overall 是同名分位数**相加**，不是跨分位数求平均
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# admin_router 住在 content-service 目录下（带连字符，不是合法包名），
# 且它 `from clients.ai_client import ...` 依赖 content-service 在 sys.path 上
# （正常由 main.py 负责）。本测试是纯函数测试、不碰 DB，所以直接把目录加进去。
_SERVICE_DIR = Path(__file__).resolve().parents[2] / "content-service"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))


def _load_parse():
    if "admin_router" in sys.modules:
        return sys.modules["admin_router"]._parse_distill_p95
    spec = importlib.util.spec_from_file_location("admin_router", _SERVICE_DIR / "admin_router.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["admin_router"] = module
    spec.loader.exec_module(module)
    return module._parse_distill_p95


parse = _load_parse()


# ── 真实抓取的本机 metrics 片段（8104 arq worker 进程）──────────────────────
# 当时桶上界是 600s，step3_tts 的真实耗时远超它。
REAL_METRICS = """
# HELP distill_step_duration_seconds Distill step latency (CP3.6, v1 §11.3)
# TYPE distill_step_duration_seconds histogram
distill_step_duration_seconds_bucket{le="0.5",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="1.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="2.5",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="5.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="10.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="30.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="60.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="120.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="300.0",step="step3_tts"} 0.0
distill_step_duration_seconds_bucket{le="600.0",step="step3_tts"} 1.0
distill_step_duration_seconds_bucket{le="+Inf",step="step3_tts"} 6.0
distill_step_duration_seconds_count{step="step3_tts"} 6.0
distill_step_duration_seconds_sum{step="step3_tts"} 4469.1278261669795
distill_step_duration_seconds_bucket{le="0.5",step="step1_structure"} 0.0
distill_step_duration_seconds_bucket{le="1.0",step="step1_structure"} 0.0
distill_step_duration_seconds_bucket{le="2.5",step="step1_structure"} 1.0
distill_step_duration_seconds_bucket{le="5.0",step="step1_structure"} 1.0
distill_step_duration_seconds_bucket{le="+Inf",step="step1_structure"} 1.0
distill_step_duration_seconds_count{step="step1_structure"} 1.0
distill_step_duration_seconds_sum{step="step1_structure"} 0.2498
distill_step_duration_seconds_bucket{le="30.0",step="step2_rewrite"} 1.0
distill_step_duration_seconds_bucket{le="60.0",step="step2_rewrite"} 1.0
distill_step_duration_seconds_bucket{le="+Inf",step="step2_rewrite"} 1.0
distill_step_duration_seconds_count{step="step2_rewrite"} 1.0
distill_step_duration_seconds_sum{step="step2_rewrite"} 20.0001
distill_step_duration_seconds_bucket{le="0.5",step="step4_concat"} 0.0
distill_step_duration_seconds_bucket{le="1.0",step="step4_concat"} 1.0
distill_step_duration_seconds_bucket{le="+Inf",step="step4_concat"} 1.0
distill_step_duration_seconds_count{step="step4_concat"} 1.0
distill_step_duration_seconds_sum{step="step4_concat"} 0.2503
"""


@pytest.fixture
def parsed():
    return parse(REAL_METRICS)


def _build_metrics(steps: list[tuple[str, tuple[float, ...], float, int]]) -> str:
    """按 (step, 桶边界, 恒定耗时, 样本数) 造一段合法的 Prometheus 文本。

    耗时落在哪个桶区间，累积计数就只从那个桶开始累加 —— 桶计数是累积的，
    不能每个桶都写满，否则第一个桶就超过 target，插值结果全错。
    """
    out = (
        "# HELP distill_step_duration_seconds Distill step latency\n"
        "# TYPE distill_step_duration_seconds histogram\n"
    )
    for step, bounds, value, n in steps:
        cum = 0
        entered = False
        for le in bounds:
            if not entered and value <= le:
                entered = True
                cum += n
            out += f'distill_step_duration_seconds_bucket{{le="{le}",step="{step}"}} {cum}\n'
        out += f'distill_step_duration_seconds_bucket{{le="+Inf",step="{step}"}} {cum}\n'
        out += f'distill_step_duration_seconds_count{{step="{step}"}} {cum}\n'
        out += f'distill_step_duration_seconds_sum{{step="{step}"}} {value * cum}\n'
    return out


class TestOutOfRangeDoesNotFabricate:
    """核心回归：超量程必须返回 None，绝不编造。"""

    def test_p50_beyond_infinite_bucket_is_none(self, parsed):
        # 6 个样本里 5 个 >600s，P50(target=3) 落在 +Inf 桶。
        # 旧实现会算出 600 + 0.4*(1e18-600) ≈ 4.0e17。
        assert parsed["by_step"]["step3_tts"]["p50"] is None

    def test_all_quantiles_beyond_infinite_bucket_are_none(self, parsed):
        step3 = parsed["by_step"]["step3_tts"]
        assert step3["p95"] is None
        assert step3["p99"] is None

    def test_no_value_is_absurdly_large(self, parsed):
        """兜底断言：任何步骤的任何数字都不该超过一天。"""
        for step, vals in parsed["by_step"].items():
            for key in ("p50", "p95", "p99", "mean"):
                v = vals.get(key)
                if v is not None:
                    assert v < 86400, f"{step}.{key} = {v} 明显不是真实耗时"

    def test_out_of_range_still_reports_truthful_mean(self, parsed):
        """分位数不可解，但均值是直方图算得出的精确量，必须留着。"""
        # 4469.1278261669795 / 6
        assert parsed["by_step"]["step3_tts"]["mean"] == pytest.approx(744.85, abs=0.01)

    def test_out_of_range_reports_count_and_upper_bound(self, parsed):
        step3 = parsed["by_step"]["step3_tts"]
        assert step3["count"] == 6
        assert step3["upper_bound"] == 600.0


class TestInRangeQuantiles:
    """分位数 = 目标桶内的线性插值，期望值按插值公式手算。

    桶是累积计数，所以「1 个样本耗时 0.25s」对应的计数序列是
    le=0.5→0, le=1.0→0, le=2.5→1, le=5.0→1；P50(target=0.5) 落在 le=2.5，
    由 (1.0,0) 插到 (2.5,1) 得 1.0 + 0.5×1.5 = 1.75。
    """

    def test_step1_median(self, parsed):
        assert parsed["by_step"]["step1_structure"]["p50"] == pytest.approx(1.75)

    def test_step2_median(self, parsed):
        # 首桶 le=30 计数已 1 ≥ target 0.5 → 0 + 0.5×30
        assert parsed["by_step"]["step2_rewrite"]["p50"] == pytest.approx(15.0)

    def test_step4_median(self, parsed):
        assert parsed["by_step"]["step4_concat"]["p50"] == pytest.approx(0.75)

    def test_mean_comes_from_sum_over_count(self, parsed):
        assert parsed["by_step"]["step2_rewrite"]["mean"] == pytest.approx(20.0001)

    def test_upper_bound_is_largest_finite_bucket(self, parsed):
        # 不是 +Inf，也不是任何插值结果，就是最后一个有限桶
        assert parsed["by_step"]["step3_tts"]["upper_bound"] == 600.0
        assert parsed["by_step"]["step1_structure"]["upper_bound"] == 5.0


class TestOverall:
    def test_overall_is_none_when_any_step_unresolvable(self, parsed):
        """4 步串行相加，缺一步就整体不可解 —— 不能拿 3 步的数和冒充整体。"""
        assert parsed["overall"]["p50"] is None
        assert parsed["overall"]["p95"] is None

    def test_overall_mean_is_sum_of_step_means(self, parsed):
        """期望的线性性：E[端到端] = Σ E[各步]，这是精确等式。"""
        expected = (
            parsed["by_step"]["step1_structure"]["mean"]
            + parsed["by_step"]["step2_rewrite"]["mean"]
            + parsed["by_step"]["step3_tts"]["mean"]
            + parsed["by_step"]["step4_concat"]["mean"]
        )
        assert parsed["overall"]["mean"] == pytest.approx(expected)

    def test_overall_is_sum_not_average(self):
        """旧实现算的是「各步同名分位数的平均」，把 P50 和 P99 混在一起求均值。

        构造两个量程内可解的 step：耗时恒为 20s 和 90s，各 4 个样本。
        端到端应该是 20 + 90 = 110；旧实现会算成 (20 + 90) / 2 = 55。
        """
        metrics = _build_metrics(
            [
                # (step, 桶边界, 真实耗时, 样本数) —— 真实耗时落在第 3 个桶区间内
                ("s_a", (5.0, 10.0, 30.0, 60.0), 20.0, 4),
                ("s_b", (30.0, 60.0, 120.0, 300.0), 90.0, 4),
            ]
        )
        out = parse(metrics)
        assert out["by_step"]["s_a"]["p50"] == pytest.approx(20.0)
        assert out["by_step"]["s_b"]["p50"] == pytest.approx(90.0)
        assert out["overall"]["p50"] == pytest.approx(110.0), "overall 必须是相加不是平均"
        assert out["overall"]["mean"] == pytest.approx(110.0)


class TestDegenerateInputs:
    def test_empty_metrics(self):
        out = parse("")
        assert out["by_step"] == {}
        assert out["overall"]["p50"] is None

    def test_garbage_text_does_not_raise(self):
        out = parse("not prometheus at all\n{{{")
        assert out["by_step"] == {}

    def test_other_families_ignored(self):
        out = parse('some_other_metric_bucket{le="1.0"} 5.0\n')
        assert out["by_step"] == {}
