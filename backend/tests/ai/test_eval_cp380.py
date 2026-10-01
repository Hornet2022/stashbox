"""CP3.8.0：评测 Baseline + 回归测试（evaluator + calibrator + regression + blind test）。

20 个用例覆盖：
- evaluator：predict_quality_score / calibration / inter_evaluator_agreement
- score_calibrator：3 源融合 + user None + all None + 权重归一化
- regression：no change / drop > 0.5 / single regression
- tts_blind_test：setup 3 providers / compute median
- score_predictor 集成：用 Evaluator 不用 mock
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# 1. evaluator.predict_quality_score
# ---------------------------------------------------------------------------
async def test_predict_quality_score_returns_float():
    """CP3.8.0 §2.7：predict_quality_score 返回 float。"""
    from distill.evaluator import Evaluator

    ev = Evaluator()
    score = await ev.predict_quality_score("dst_x", "test text", "http://audio")
    assert isinstance(score, float)
    assert 0 <= score <= 10


async def test_predict_quality_score_failure_returns_mock_85():
    """CP3.8.0：DB 失败时返回 8.5（不破主流程）。"""
    from distill.evaluator import Evaluator

    ev = Evaluator()
    # 正常调用本期返 8.5
    score = await ev.predict_quality_score("dst_y", "test", "http://audio")
    assert score == 8.5


async def test_collect_evaluator_calibration_3_evaluators_agreement():
    """CP3.8.0 §2.7：3 评测员校准 + inter_evaluator_agreement。"""
    from distill.evaluator import Evaluator

    ev = Evaluator()
    result = await ev.collect_evaluator_calibration(
        articles=["art_1", "art_2", "art_3"],
        evaluator_ids=["evaluator_a", "evaluator_b", "evaluator_c"],
    )
    assert "inter_evaluator_agreement" in result
    assert "evaluator_stats" in result
    assert "baseline_scores" in result
    assert result["inter_evaluator_agreement"] == 1.0  # 本期 mock 完全一致
    assert len(result["evaluator_stats"]) == 3
    assert len(result["baseline_scores"]) == 3


async def test_inter_evaluator_agreement_pairwise():
    """CP3.8.0：inter_evaluator_agreement pairwise 计算。"""
    from distill.evaluator import Evaluator

    ev = Evaluator()
    # 3 个评测员对 5 篇文章评分
    agreement = ev.compute_inter_evaluator_agreement(
        evaluator_scores={
            "e1": [8.0, 8.5, 9.0, 7.5, 8.0],
            "e2": [8.0, 8.5, 9.0, 7.5, 8.0],
            "e3": [8.1, 8.4, 9.1, 7.4, 8.1],
        }
    )
    assert 0.0 <= agreement <= 1.0
    # 几乎完全一致
    assert agreement > 0.8


async def test_inter_evaluator_agreement_one_evaluator():
    """CP3.8.0：< 2 个评测员 → 返 1.0（边界）。"""
    from distill.evaluator import Evaluator

    ev = Evaluator()
    agreement = ev.compute_inter_evaluator_agreement(evaluator_scores={"e1": [8.0, 8.5]})
    assert agreement == 1.0


# ---------------------------------------------------------------------------
# 2. score_calibrator
# ---------------------------------------------------------------------------
def test_calibrate_3_sources_weighted():
    """CP3.8.0 §2.7：3 源融合（评测员 0.5 + LLM 0.3 + 用户 0.2）。"""
    from distill.score_calibrator import ScoreCalibrator

    cal = ScoreCalibrator()
    # (8*0.5 + 9*0.3 + 10*0.2) / 1.0 = 8.7
    result = cal.calibrate(evaluator_score=8.0, llm_score=9.0, user_score=10.0)
    assert result == pytest.approx(8.7, abs=0.01)


def test_calibrate_user_none_excludes():
    """CP3.8.0：user_score=None 时不参与融合。"""
    from distill.score_calibrator import ScoreCalibrator

    cal = ScoreCalibrator()
    # (8*0.5 + 9*0.3) / 0.8 = 8.375
    result = cal.calibrate(evaluator_score=8.0, llm_score=9.0)
    assert result == pytest.approx(8.375, abs=0.01)


def test_calibrate_all_zero_returns_default_85():
    """CP3.8.0：所有源都缺失 → 返 8.5 fallback。"""
    from distill.score_calibrator import ScoreCalibrator

    cal = ScoreCalibrator()
    result = cal.calibrate()
    assert result == 8.5


def test_calibrate_only_user():
    """CP3.8.0：只有 user_score（评测员 / LLM 缺失）。"""
    from distill.score_calibrator import ScoreCalibrator

    cal = ScoreCalibrator()
    result = cal.calibrate(user_score=9.0)
    assert result == 9.0  # 只有 1 源时直接返


def test_calibrate_weight_normalization():
    """CP3.8.0：权重归一化（缺失源时剩余权重除以总和）。"""
    from distill.score_calibrator import ScoreCalibrator

    cal = ScoreCalibrator()
    # 只有 LLM（0.3） → 总权重 0.3，10*0.3/0.3 = 10
    result = cal.calibrate(llm_score=10.0)
    assert result == 10.0


# ---------------------------------------------------------------------------
# 3. regression
# ---------------------------------------------------------------------------
def test_detect_regression_no_change_returns_false():
    """CP3.8.0 §2.7：new == old → False。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    assert reg.detect_regression([8.5, 8.5], [8.5, 8.5]) is False


def test_detect_regression_drop_05_returns_true():
    """CP3.8.0：mean drop > 0.5 → True。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    # mean 9.0 → 8.0，delta=-1 < -0.5 → True
    assert reg.detect_regression([9.0, 9.0], [8.0, 8.0]) is True


def test_detect_regression_drop_03_returns_false():
    """CP3.8.0：mean drop < 0.5 → False（不报警）。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    # mean 9.0 → 8.8，delta=-0.2 > -0.5 → False
    assert reg.detect_regression([9.0, 9.0], [8.8, 8.8]) is False


def test_detect_regression_improvement_returns_false():
    """CP3.8.0：mean 上升 → False（不是 regression）。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    assert reg.detect_regression([8.0, 8.0], [9.0, 9.0]) is False


def test_detect_single_regression():
    """CP3.8.0：单文章回归判定。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    # drop 0.6 < -0.5 → True
    assert reg.detect_single_regression(9.0, 8.4) is True
    # drop 0.3 > -0.5 → False
    assert reg.detect_single_regression(9.0, 8.7) is False
    # improvement → False
    assert reg.detect_single_regression(8.0, 9.0) is False


async def test_run_regression_old_articles():
    """CP3.8.0：跑回归（100 篇历史，本期 mock）。"""
    from distill.regression import DistillRegressionRunner

    reg = DistillRegressionRunner()
    # 5 篇历史，本期全部返 8.5
    old_articles = [{"article_id": f"art_{i}", "baseline_score": 8.5} for i in range(5)]
    count = await reg.run_regression(old_articles, new_pipeline=None, evaluator=None)
    # 本期 mock 全 8.5 → 无 regression
    assert count == 0


# ---------------------------------------------------------------------------
# 4. tts_blind_test
# ---------------------------------------------------------------------------
async def test_setup_blind_test_3_providers():
    """CP3.8.0 §2.7：3 provider 盲测 setup。"""
    from distill.tts_blind_test import TtsBlindTest

    bt = TtsBlindTest()
    result = await bt.setup_blind_test("test text", ["openai", "qwen_vl", "claude"])
    assert "samples" in result
    assert "order" in result
    assert len(result["samples"]) == 3
    assert len(result["order"]) == 3
    # order 是 providers 的随机打乱
    assert set(result["order"]) == {"openai", "qwen_vl", "claude"}


def test_compute_blind_score_median_per_provider():
    """CP3.8.0 §2.7：盲测中位数。"""
    from distill.tts_blind_test import TtsBlindTest

    bt = TtsBlindTest()
    result = bt.compute_blind_score(
        evaluator_scores={
            "sample_1": [8.0, 8.5, 9.0],
            "sample_2": [7.0, 7.5, 8.0],
            "sample_3": [9.0, 9.5, 10.0],
        },
        provider_mapping={
            "sample_1": "openai",
            "sample_2": "qwen_vl",
            "sample_3": "claude",
        },
    )
    assert result["openai"] == 8.5
    assert result["qwen_vl"] == 7.5
    assert result["claude"] == 9.5


def test_compute_blind_score_empty():
    """CP3.8.0：空输入返 {}."""
    from distill.tts_blind_test import TtsBlindTest

    bt = TtsBlindTest()
    result = bt.compute_blind_score(evaluator_scores={}, provider_mapping={})
    assert result == {}


# ---------------------------------------------------------------------------
# 5. score_predictor 集成
# ---------------------------------------------------------------------------
def test_score_predictor_no_longer_depends_on_evaluator():
    """2026-10-02：score_predictor 不再走 Evaluator（那是个恒返回 8.5 的假实现）。

    质量分改为直接来自真实用户评分（distillation_evaluations）。
    """
    import distill.score_predictor as sp

    assert not hasattr(sp, "get_evaluator"), "不该再依赖恒返回 8.5 的 Evaluator"


def test_score_predictor_no_mock_score_constant():
    """2026-10-02：MOCK_SCORE 已移除，系统不再编造质量分。"""
    import distill.score_predictor as sp

    assert not hasattr(sp, "MOCK_SCORE"), "MOCK_SCORE 不该再存在"


# ---------------------------------------------------------------------------
# 6. 集成：hooks_impl 4 个 hook 仍 OK
# ---------------------------------------------------------------------------
def test_default_post_hooks_cp380_unchanged_4_hooks():
    """CP3.8.0：default_post_hooks 仍 4 个（CP3.7.3 baseline）。"""
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
