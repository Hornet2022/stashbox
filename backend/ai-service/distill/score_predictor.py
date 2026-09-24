"""CP3.8.0 §2.2.E：ScorePredictorHook（听感评分预测）。

按 docs/听感产品化方案_v1.md §2.2.E + §2.7 评测体系：
- CP3.7.3：mock 8.5
- CP3.8.0：调 Evaluator.predict_quality_score（融合 3 源）
- 失败兑底：异常 → 返 8.5
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .evaluator import Evaluator

log = structlog.get_logger("distill.score_predictor")

# CP3.7.3 保留：mock fallback 评分
MOCK_SCORE = 8.5

# CP3.8.0：单例 Evaluator（CP3.6.2 factory 单例风格）
_evaluator_singleton: Evaluator | None = None


def get_evaluator() -> Evaluator:
    """CP3.8.0：Evaluator 单例（CP3.6.2 factory 风格）。"""
    global _evaluator_singleton
    if _evaluator_singleton is None:
        _evaluator_singleton = Evaluator()
    return _evaluator_singleton


async def predict_and_save_quality_score(
    db: AsyncSession,
    distilled_article_id: str,
) -> float:
    """CP3.8.0：预测听感评分并写入 distilled_articles.quality_score。

    CP3.7.3：mock 8.5
    CP3.8.0：调 Evaluator.predict_quality_score（融合评测员 + LLM + 启发式 3 源）

    Returns:
        quality_score（float，0-10，fallback 8.5）
    """
    try:
        # 取 rewrite_text + audio_url from distilled_articles
        from stashbox.backend.common.models import DistilledArticle

        article = await db.scalar(
            select(DistilledArticle).where(DistilledArticle.id == distilled_article_id)
        )
        rewrite_text = ""
        audio_url = ""
        if article is not None:
            audio_url = getattr(article, "audio_url", "") or ""
            rewrite_text = (article.title or "") if article else ""

        # CP3.8.0：调 Evaluator.predict_quality_score（融合 3 源）
        evaluator = get_evaluator()
        score = await evaluator.predict_quality_score(distilled_article_id, rewrite_text, audio_url)

        # 写回 DB
        await db.execute(
            update(DistilledArticle)
            .where(DistilledArticle.id == distilled_article_id)
            .values(quality_score=score)
        )
        log.info(
            "score_predictor_written",
            article_id=distilled_article_id,
            score=score,
            source="evaluator_3_sources",
        )
        return score
    except Exception as e:
        # CP3.7.3：DB 失败时返回 MOCK_SCORE（不破主流程）
        log.warning(
            "score_predictor_failed_continue",
            article_id=distilled_article_id,
            error=str(e),
        )
        return MOCK_SCORE
