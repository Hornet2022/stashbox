"""CP3.8.0 §2.2.E：ScorePredictorHook（听感评分预测）。

## 2026-10-02 重写：不再写假分

原来的实现有三处问题叠在一起：

1. ``MOCK_SCORE = 8.5`` 被当作 fallback 写进 ``distilled_articles.quality_score``；
2. ``Evaluator.predict_quality_score`` 的「3 源融合」里三个源都写死 8.5，
   加权结果恒等于 8.5 —— 是演给人看的，不是算出来的；
3. 查库用 ``DistilledArticle.id == task_id``，但 agent 路径是按
   ``article_id`` 落业务键的，多跑几次就查不到那一行。

**写 8.5 比写 NULL 更有害**：NULL 读作「尚未评估」，8.5 读作「评估过了，
质量不错」—— 是一句会被下游当真的话。管理后台的评分列、按分排序过滤、
A/B 报表全都会拿它当依据。

现在的口径：只用**真实存在的用户听感评分**（``distillation_evaluations``
里的 4 维分，取其均值）；没有真实评分就返回 None，字段留 NULL。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("distill.score_predictor")


async def _real_score_from_evaluations(db: AsyncSession, article_id: str) -> float | None:
    """从真实用户评分里取这篇的质量分（0-10 制），没有就 None。

    数据源：``distillation_evaluations.overall_score``（1-5 分制，真实用户提交），
    折算到 0-10：``overall * 2``。

    ## 关联键为什么不能直接用 ctx.task_id

    ``distillation_evaluations`` 表里**没有 article_id 列**，只有 task_id；写入时
    用的是 ``task_id = da.id``（见 ``evaluation_service.py``），而 ``da.id`` 是这篇
    文章**首次**蒸馏时生成的那个 task_id。文章一旦重蒸，当前这次 ``ctx.task_id``
    就和它对不上 —— 直接按 ctx.task_id 查会永远查不到评分。

    所以走桥接：article_id → DistilledArticle.id → evaluations.task_id。
    """
    from stashbox.backend.common.models import DistillationEvaluation, DistilledArticle

    try:
        da_id = await db.scalar(
            select(DistilledArticle.id).where(DistilledArticle.article_id == article_id)
        )
        if da_id is None:
            log.info("quality_score_no_distilled_row", article_id=article_id)
            return None

        result = await db.execute(
            select(DistillationEvaluation.overall_score).where(
                DistillationEvaluation.task_id == da_id
            )
        )
        # 只有 execute 是协程；.scalars() / .all() 都是同步的（实测 SQLAlchemy 2.x）
        rows = result.scalars().all()
    except Exception as exc:
        log.warning("quality_score_real_eval_query_failed", article_id=article_id, error=str(exc))
        return None

    scores = [float(s) for s in rows if s is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores) * 2, 2)


async def predict_and_save_quality_score(
    db: AsyncSession,
    article_id: str,
) -> float | None:
    """用**真实**用户听感评分算 quality_score 并落库；没有真实评分则不写。

    Args:
        db: 数据库会话
        article_id: 业务键（articles.id），不是 task_id

    Returns:
        写入的 quality_score（0-10），无真实评分时返回 None
    """
    from stashbox.backend.common.models import DistilledArticle

    score = await _real_score_from_evaluations(db, article_id)
    if score is None:
        log.info(
            "quality_score_skipped_no_real_eval",
            article_id=article_id,
            reason="没有真实用户听感评分，保持 NULL（不写假分）",
        )
        return None

    try:
        await db.execute(
            update(DistilledArticle)
            .where(DistilledArticle.article_id == article_id)
            .values(quality_score=score)
        )
        log.info(
            "quality_score_written",
            article_id=article_id,
            score=score,
            source="real_user_evaluations",
        )
        return score
    except Exception as e:
        log.warning(
            "quality_score_write_failed_continue",
            article_id=article_id,
            error=str(e),
        )
        return None
