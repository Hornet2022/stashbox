"""CP3.8.0 §2.7：评分校准器（评测员 + LLM + 用户三方融合）。

按 docs/听感产品化方案_v1.md §2.7 严格实现：
- 评测员：权重 0.5（最权威，3-5 个评测员打分中位数）
- LLM：权重 0.3（GPT-4o 二次评估，模拟启发式）
- 用户：权重 0.2（来自 Android 4 维评分，None 则不参与）
"""

from __future__ import annotations

import structlog
from typing import Optional

log = structlog.get_logger("distill.score_calibrator")

# 权重（§2.7 严格按文档）
WEIGHT_EVALUATOR = 0.5
WEIGHT_LLM = 0.3
WEIGHT_USER = 0.2

# Fallback score（无任何评分时）
DEFAULT_SCORE = 8.5


class ScoreCalibrator:
    """CP3.8.0 §2.7：评分校准器（3 源融合）。"""

    def calibrate(
        self,
        evaluator_score: Optional[float] = None,
        llm_score: Optional[float] = None,
        user_score: Optional[float] = None,
    ) -> float:
        """CP3.8.0 §2.7 三方融合校准。

        公式：
        - 评测员（None 视为缺失）：权重 0.5
        - LLM（None 视为缺失）：权重 0.3
        - 用户（None 视为缺失）：权重 0.2

        最终分 = sum(score * weight) / sum(weight)（归一化）

        Returns:
            0-10 之间的 float（fallback 8.5）
        """
        scores_weights = []
        if evaluator_score is not None:
            scores_weights.append((evaluator_score, WEIGHT_EVALUATOR))
        if llm_score is not None:
            scores_weights.append((llm_score, WEIGHT_LLM))
        if user_score is not None:
            scores_weights.append((user_score, WEIGHT_USER))

        if not scores_weights:
            log.warning("score_calibrator_no_input_returns_default")
            return DEFAULT_SCORE

        weighted_sum = sum(score * weight for score, weight in scores_weights)
        total_weight = sum(weight for _, weight in scores_weights)
        calibrated = weighted_sum / total_weight

        log.info(
            "score_calibrator_calibrated",
            evaluator=evaluator_score,
            llm=llm_score,
            user=user_score,
            result=calibrated,
            total_weight=total_weight,
        )
        return calibrated
