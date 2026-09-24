"""B2 / 缺口 A2·A3·A5·A7：ai-service admin 只读端点（听感产品化运营看板后端）。

仿 content-service/admin_router.py 拆分模式：main.py `include_router(admin_router)`，
路径为完整 /api/v1/admin/*（不带 prefix）。鉴权统一 require_admin_or_operator。

只读端点不写 admin_operation_logs（对齐 content-service 查询类现状）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import get_db
from stashbox.backend.common.models import (
    ArticleAudioVariant,
    ConsentRecord,
    DistillationEvaluation,
    DistilledArticle,
    FewShotExample,
)

from distill.ab_report import compute_ab_report
from distill.pool_health import PoolHealthMonitor

router = APIRouter()


def _clamp_limit(limit: int, cap: int = 200) -> int:
    return max(1, min(limit, cap))


# ---------------------------------------------------------------------------
# A2 · few-shot 池
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/few-shot-pool/health")
async def admin_few_shot_health(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """池健康度（高/中/低分布 + 过期率 + health_score 0-100 + warning）。

    直调 PoolHealthMonitor.compute_health —— 内部已兜底（异常返空报告），
    故本端点恒 200。
    """
    report = await PoolHealthMonitor().compute_health(db)
    return report.model_dump()


@router.get("/api/v1/admin/few-shot-pool")
async def admin_few_shot_list(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    kind: str | None = None,
    min_score: float | None = None,
    active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """池条目分页（kind=hook/section/outro；min_score 过滤；active 过滤）。"""
    limit = _clamp_limit(limit)
    conds = []
    if kind:
        conds.append(FewShotExample.kind == kind)
    if min_score is not None:
        conds.append(FewShotExample.score_avg >= min_score)
    if active is not None:
        conds.append(FewShotExample.active == active)

    q = select(FewShotExample).order_by(FewShotExample.score_avg.desc())
    cq = select(func.count()).select_from(FewShotExample)
    for c in conds:
        q = q.where(c)
        cq = cq.where(c)

    total = await db.scalar(cq) or 0
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "kind": r.kind,
                "source_pattern": r.source_pattern,
                "rewrite_text": (r.rewrite_text or "")[:120],
                "score_avg": r.score_avg,
                "usage_count": r.usage_count,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "active": r.active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# A3（读）· 蒸馏评分查询
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/evaluations")
async def admin_evaluations_list(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    task_id: str | None = None,
    user_id: int | None = None,
    auto_flag: bool | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """distillation_evaluations 分页（评测员/用户/LLM 自评混排，按时间倒序）。"""
    limit = _clamp_limit(limit)
    conds = []
    if task_id:
        conds.append(DistillationEvaluation.task_id == task_id)
    if user_id is not None:
        conds.append(DistillationEvaluation.user_id == user_id)
    if auto_flag is not None:
        conds.append(DistillationEvaluation.auto_flag == auto_flag)
    if min_score is not None:
        conds.append(DistillationEvaluation.overall_score >= min_score)
    if max_score is not None:
        conds.append(DistillationEvaluation.overall_score <= max_score)

    q = select(DistillationEvaluation).order_by(DistillationEvaluation.created_at.desc())
    cq = select(func.count()).select_from(DistillationEvaluation)
    for c in conds:
        q = q.where(c)
        cq = cq.where(c)

    total = await db.scalar(cq) or 0
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "task_id": r.task_id,
                "user_id": r.user_id,
                "hook_score": r.hook_score,
                "section_score": r.section_score,
                "outro_score": r.outro_score,
                "rhythm_score": r.rhythm_score,
                "overall_score": r.overall_score,
                "skip_reason": r.skip_reason,
                "auto_flag": r.auto_flag,
                "retried_task_id": r.retried_task_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# A5 · 多码率变体统计
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/audio-variants/stats")
async def admin_audio_variants_stats(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """变体覆盖统计：按码率分布 + 覆盖文章数 / done 文章数。

    给 CP7.4 预加载策略调优做输入（哪档被生成、平均体积）。
    """
    by_bitrate_rows = (
        await db.execute(
            select(
                ArticleAudioVariant.bitrate,
                func.count(),
                func.avg(ArticleAudioVariant.file_size_bytes),
                func.avg(ArticleAudioVariant.duration_sec),
            )
            .where(ArticleAudioVariant.deleted_at.is_(None))
            .group_by(ArticleAudioVariant.bitrate)
            .order_by(ArticleAudioVariant.bitrate.desc())
        )
    ).all()

    # 覆盖：有 ≥1 变体的 distinct distilled_article / done 且有 audio_url 的文章数
    covered = (
        await db.scalar(
            select(func.count(func.distinct(ArticleAudioVariant.distilled_article_id))).where(
                ArticleAudioVariant.deleted_at.is_(None)
            )
        )
        or 0
    )
    done_total = (
        await db.scalar(
            select(func.count())
            .select_from(DistilledArticle)
            .where(
                DistilledArticle.status == "done",
                DistilledArticle.audio_url.isnot(None),
            )
        )
        or 0
    )
    coverage_ratio = round(covered / done_total, 4) if done_total else 0.0

    return {
        "by_bitrate": [
            {
                "bitrate": bitrate,
                "count": int(cnt),
                "avg_file_size_bytes": round(float(avg_size or 0), 1),
                "avg_duration_sec": round(float(avg_dur or 0), 1),
            }
            for bitrate, cnt, avg_size, avg_dur in by_bitrate_rows
        ],
        "covered_articles": covered,
        "done_articles": done_total,
        "coverage_ratio": coverage_ratio,
    }


# ---------------------------------------------------------------------------
# A7 · consent 抽查
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/consents")
async def admin_consents_list(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    personalization_enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """GDPR 同意记录抽查（合规审计）。

    隐私：仅结构化开关 + 版本 + 时间，无自由文本字段可回显。
    """
    limit = _clamp_limit(limit)
    conds = []
    if personalization_enabled is not None:
        conds.append(ConsentRecord.personalization_enabled == personalization_enabled)

    q = select(ConsentRecord).order_by(ConsentRecord.user_id)
    cq = select(func.count()).select_from(ConsentRecord)
    for c in conds:
        q = q.where(c)
        cq = cq.where(c)

    total = await db.scalar(cq) or 0
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "user_id": r.user_id,
                "personalization_enabled": r.personalization_enabled,
                "cross_user_share_enabled": r.cross_user_share_enabled,
                "consent_version": r.consent_version,
                "consent_at": r.consent_at,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# B4 · 缺口 A4：A/B 实验报表
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/ab-report")
async def admin_ab_report(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """A/B 分组四指标（复听率/完听率/评分均值/跳过率），方案 §2.7-D。

    date_from / date_to 按蒸馏任务 created_at 过滤（ISO 8601）。
    ab_group=NULL 的历史行归入 pre_experiment 组（不可用于实验结论）。
    """
    return await compute_ab_report(db, date_from=date_from, date_to=date_to)
