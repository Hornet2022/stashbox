"""CP3.7.3 §2.2.E：ScorePredictorHook（听感评分预测）。

按 docs/听感产品化方案_v1.md §2.2.E + §2.7 评测体系：
- CP3.7.3：本期 mock 8.5（与 pipeline.py 旧值一致）
- CP3.8.0：接真实评分（评测员 / 用户评分 / LLM 评分）
"""

from __future__ import annotations

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import DistilledArticle

log = structlog.get_logger("distill.score_predictor")

# CP3.7.3：mock 评分（与 pipeline.py 旧 MOCK_QUALITY_SCORE = 8.5 一致）
MOCK_SCORE = 8.5


async def predict_and_save_quality_score(
    db: AsyncSession,
    distilled_article_id: str,
) -> float:
    """CP3.7.3：预测听感评分（mock 8.5）并写入 distilled_articles.quality_score。

    CP3.8.0：替换为真实评分（评测组 ground truth / 用户评分回灌 / LLM 评分）。

    Returns:
        quality_score（float，0-10）
    """
    try:
        await db.execute(
            update(DistilledArticle)
            .where(DistilledArticle.id == distilled_article_id)
            .values(quality_score=MOCK_SCORE)
        )
        log.info(
            "score_predictor_mock_written",
            article_id=distilled_article_id,
            score=MOCK_SCORE,
        )
        return MOCK_SCORE
    except Exception as e:
        log.warning(
            "score_predictor_failed_continue",
            article_id=distilled_article_id,
            error=str(e),
        )
        return MOCK_SCORE
