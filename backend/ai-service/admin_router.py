"""B2/B3/B4 · ai-service admin 端点（听感产品化运营看板后端）。

- B2：5 只读端点（A2/A3读/A5/A7）
- B4：GET /admin/ab-report（A4）
- B3：tier-config（A1）/ annotate+agreement（A3写）/ cleanup+audit（A6）/ blind-test（A8）

仿 content-service/admin_router.py 拆分模式：main.py `include_router(admin_router)`，
路径为完整 /api/v1/admin/*（不带 prefix）。鉴权统一 require_admin_or_operator。

只读端点不写 admin_operation_logs（对齐 content-service 查询类现状）；
写端点的审计由 api-gateway 的 AuditMiddleware 统一覆盖（/api/v1/admin/* 写方法自动记录）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import InvalidRequest, NotFound
from stashbox.backend.common.models import (
    ArticleAudioVariant,
    ConsentRecord,
    DistillationEvaluation,
    DistilledArticle,
    FewShotExample,
)

from distill.ab_report import compute_ab_report
from distill.evaluation_service import validate_evaluation_payload
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


# ---------------------------------------------------------------------------
# B3 · A1：tier→model 映射热改（system_config KEY_TIER，DB > 代码默认）
# ---------------------------------------------------------------------------

_SUPPORTED_LLM_PROVIDERS = ("openai", "qwen_vl")  # llm/factory 实际实现 client 的 provider


@router.get("/api/v1/admin/tier-config")
async def admin_tier_config_get(
    user: dict = Depends(require_admin_or_operator),
):
    """生效 tier 映射 + 来源（db/default）+ 代码默认 + provider 覆盖 warning。

    ⚠️ 不含任何 key 字段（方案 §4-3 密钥红线）。
    """
    from distill.tier_router import TIER_MODEL_MAP, resolve_tier_map

    effective, source = await resolve_tier_map()
    warnings = [
        f"provider '{p}' 已配置但 llm/factory 未实现 client，配置不会生效"
        for tier_map in effective.values()
        for p in tier_map
        if p not in _SUPPORTED_LLM_PROVIDERS
    ]

    # 动作 1（2026-09-24 事故后加）：提示「模型名与当前 LLM 供应商不符」。
    # 事故背景：llm 换成火山方舟 + doubao-*，tier 却留着 gpt-4o →
    # 蒸馏调用 LLM 报 404 UnsupportedModel，整条链路 100% 失败。
    try:
        from stashbox.backend.common.system_config import KEY_LLM, get_config

        from distill.tier_router import check_model_matches_vendor

        llm_cfg = await get_config(KEY_LLM) or {}
        active_provider = str(llm_cfg.get("provider") or "openai").lower()
        active_base_url = llm_cfg.get("base_url")
        for tier_map in effective.values():
            for prov, model in tier_map.items():
                err = check_model_matches_vendor(prov, model, active_provider, active_base_url)
                if err:
                    warnings.append(err)
    except Exception:
        pass  # 读不到 llm 配置时不提示（不阻塞 GET）

    return {
        "tier_model_map": effective,
        "source": source,
        "default_map": TIER_MODEL_MAP,
        "supported_providers": list(_SUPPORTED_LLM_PROVIDERS),
        "warnings": sorted(set(warnings)),
    }


class TierConfigPutRequest(BaseModel):
    tier_model_map: dict[str, dict[str, str]] = Field(min_length=1)


@router.put("/api/v1/admin/tier-config")
async def admin_tier_config_put(
    req: TierConfigPutRequest,
    user: dict = Depends(require_admin_or_operator),
):
    """写 system_config KEY_TIER（结构校验同 resolve_tier_map）。

    生效方式：蒸馏任务启动前已有 reload 链路，tier map 在 resolve 时读
    system_config（Redis 5s 缓存 + 写后立即 invalidate）→ 无需重启。
    """
    from stashbox.backend.common.system_config import KEY_TIER, set_config

    from distill.tier_router import _validate_tier_map

    validated = _validate_tier_map(req.tier_model_map)
    if validated is None:
        raise InvalidRequest("tier_model_map 结构非法：须为 {simple|full: {provider: 非空模型名}}")
    for tier_map in validated.values():
        for model in tier_map.values():
            if len(model) > 128:
                raise InvalidRequest("模型名过长（≤128 字符）")

    # 动作 1（2026-09-24 事故后加）：模型名 ↔ 当前 LLM 供应商一致性校验。
    # 背景：管理后台把 llm 配成火山方舟（doubao-*），tier 却配 gpt-4o →
    # 蒸馏调用 LLM 报 404 UnsupportedModel，整条蒸馏链 100% 失败。
    # 这里在**保存时**就拦掉，而不是等蒸馏跑挂。供应商无法判断时放行。
    from stashbox.backend.common.system_config import KEY_LLM, get_config

    from distill.tier_router import check_model_matches_vendor

    try:
        llm_cfg = await get_config(KEY_LLM) or {}
    except Exception:
        llm_cfg = {}
    active_provider = str(llm_cfg.get("provider") or "openai").lower()
    active_base_url = llm_cfg.get("base_url")
    conflicts = [
        err
        for tier_map in validated.values()
        for prov, model in tier_map.items()
        if (err := check_model_matches_vendor(prov, model, active_provider, active_base_url))
    ]
    if conflicts:
        raise InvalidRequest(conflicts[0])

    updated_by = int(user.get("sub") or 0) or None
    row = await set_config(KEY_TIER, {"tier_model_map": validated}, updated_by=updated_by)
    return {
        "tier_model_map": validated,
        "source": "db",
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ---------------------------------------------------------------------------
# B3 · A3（写）：评测员标注 + 评测员间一致性
# ---------------------------------------------------------------------------


class AnnotateRequest(BaseModel):
    hook_score: int | None = None
    section_score: int | None = None
    outro_score: int | None = None
    rhythm_score: int | None = None
    overall_score: int
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/api/v1/admin/evaluations/{evaluation_id}/annotate")
async def admin_evaluation_annotate(
    evaluation_id: str,
    req: AnnotateRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """评测员对已有用户评分做校准标注（写新行，evaluator_id=admin sub）。

    口径（决策 §5.2 方案 B）：user_id 沿用被标注行的用户（保持「该任务评分人」语义），
    evaluator_id 记录评测员归属；不修改原行。入池/画像联动不触发（标注不是用户评分）。
    """
    validate_evaluation_payload(req)

    original = await db.get(DistillationEvaluation, evaluation_id)
    if original is None:
        raise NotFound(f"评分 {evaluation_id} 不存在")

    admin_id = int(user.get("sub") or 0) or None
    if admin_id is None:
        raise InvalidRequest("admin token 缺少 sub，无法记录评测员归属")

    now = datetime.now()
    row = DistillationEvaluation(
        id=f"eval_{uuid.uuid4().hex[:24]}",
        task_id=original.task_id,
        user_id=original.user_id,
        hook_score=req.hook_score,
        section_score=req.section_score,
        outro_score=req.outro_score,
        rhythm_score=req.rhythm_score,
        overall_score=req.overall_score,
        comment=req.comment,
        auto_flag=False,
        evaluator_id=admin_id,
        # SQLite 测试兼容：模型 created_at server_default 是字符串 "now()"，
        # 显式传值绕开 RETURNING 解析（同 evaluation_service B1 口径）
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    return {
        "id": row.id,
        "annotates": evaluation_id,
        "task_id": row.task_id,
        "evaluator_id": admin_id,
        "overall_score": row.overall_score,
    }


@router.get("/api/v1/admin/evaluations/agreement")
async def admin_evaluations_agreement(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    task_id: str | None = None,
):
    """评测员间一致性（0-1）。按 (evaluator_id, task) 组织 overall_score 序列。

    纯聚合：只统计有 evaluator_id 的标注行（校准标注），排除用户评分。
    """
    from distill.evaluator import Evaluator

    conds = [
        DistillationEvaluation.evaluator_id.isnot(None),
        DistillationEvaluation.deleted_at.is_(None),
    ]
    if task_id:
        conds.append(DistillationEvaluation.task_id == task_id)
    rows = (
        await db.execute(
            select(
                DistillationEvaluation.evaluator_id,
                DistillationEvaluation.task_id,
                DistillationEvaluation.overall_score,
            )
            .where(*conds)
            .order_by(DistillationEvaluation.task_id, DistillationEvaluation.created_at)
        )
    ).all()
    per_evaluator: dict[str, list[float]] = {}
    for evaluator_id, _tid, score in rows:
        per_evaluator.setdefault(str(evaluator_id), []).append(float(score))

    agreement = Evaluator().compute_inter_evaluator_agreement(per_evaluator)
    return {
        "agreement": agreement,
        "evaluator_count": len(per_evaluator),
        "annotated_count": len(rows),
        "task_filter": task_id,
    }


# ---------------------------------------------------------------------------
# B3 · A6：few-shot 池运营（cleanup / 抽查采样 / 抽查结果回写）
# ---------------------------------------------------------------------------


@router.post("/api/v1/admin/few-shot-pool/cleanup")
async def admin_few_shot_cleanup(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """手动跑完整清理（stale + low_quality + duplicates）。

    各子清理内部 try/except 自兜底；决策 §5.4：先手动端点，观察一周再定 cron。
    """
    from distill.pool_cleanup import PoolCleanupService

    return await PoolCleanupService().run_full_cleanup(db)


@router.get("/api/v1/admin/few-shot-pool/audit-sample")
async def admin_few_shot_audit_sample(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    size: int = 10,
):
    """按 50/30/20 高/中/低分策略抽池子样本供人工评审。"""
    from distill.pool_audit import PoolAuditService

    size = _clamp_limit(size, cap=50)
    examples = await PoolAuditService().select_for_audit(db, sample_size=size)
    return {
        "total": len(examples),
        "items": [
            {
                "id": ex.id,
                "kind": ex.kind,
                "source_pattern": ex.source_pattern,
                "rewrite_text": (ex.rewrite_text or "")[:200],
                "score_avg": ex.score_avg,
                "usage_count": ex.usage_count,
            }
            for ex in examples
        ],
    }


class AuditResultRequest(BaseModel):
    example_id: str
    audit_score: float = Field(ge=0, le=10)


@router.post("/api/v1/admin/few-shot-pool/audit-result")
async def admin_few_shot_audit_result(
    req: AuditResultRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """人工抽查结果回写 score_avg（加权平均）。auditor = admin sub。"""
    from distill.pool_audit import PoolAuditService

    ok = await PoolAuditService().record_audit_result(
        db, req.example_id, req.audit_score, auditor_id=str(user.get("sub") or "admin")
    )
    if not ok:
        raise NotFound(f"few-shot 条目 {req.example_id} 不存在或回写失败")
    return {"example_id": req.example_id, "audit_score": req.audit_score, "updated": True}


# ---------------------------------------------------------------------------
# B3 · A8：TTS 盲测（进程内会话存储；重启丢失，评测周期内可接受）
# ---------------------------------------------------------------------------

_BLIND_TEST_TTL_SEC = 24 * 3600
_blind_test_sessions: dict[str, dict] = {}  # id -> {samples, order, scores, created_at}


def _blind_gc() -> None:
    now = datetime.now()
    expired = [
        k
        for k, v in _blind_test_sessions.items()
        if (now - v["created_at"]).total_seconds() > _BLIND_TEST_TTL_SEC
    ]
    for k in expired:
        _blind_test_sessions.pop(k, None)


class BlindTestSetupRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    providers: list[str] = Field(min_length=2, max_length=6)


@router.post("/api/v1/admin/tts/blind-test")
async def admin_tts_blind_test_setup(
    req: BlindTestSetupRequest,
    user: dict = Depends(require_admin_or_operator),
):
    """发起盲测：同文本 × N provider **真合成** + 随机隐藏映射。

    2026-10-02：setup 从 mock 假 URL 改成真调 TTS。原先它返回
    `https://tts-blind-test.example/...`，评测员听不到任何声音，逐条打的
    1-5 分在揭晓后全部作废 —— 功能上是废的。音频现在落到
    `/tmp/audio/blind-test/`，由 api-gateway 的 `/audio/*` 挂载出去可播。

    盲测会话存进程内存（单 worker 前提，重启即失）——这是为了不把 provider
    映射写进库里：写库就等于给评测员开了后门。
    """
    from distill.tts_blind_test import TtsBlindTest

    _blind_gc()
    result = await TtsBlindTest().setup_blind_test(req.text, req.providers)
    samples = result.get("samples") or {}
    if not samples:
        raise InvalidRequest("盲测样本生成失败（setup 返回空）")

    blind_id = f"bt_{uuid.uuid4().hex[:16]}"
    # TtsBlindTest.setup 语义：sample_{i+1} ↔ providers[i]（原始列表）；
    # 返回的 order 只是随机展示序，不能作为反查映射 → 保存原始 providers
    _blind_test_sessions[blind_id] = {
        "samples": samples,
        "providers": list(req.providers),
        "order": result["order"],
        "created_at": datetime.now(),
    }
    # 样本 URL 直接用真实路径 —— TtsBlindTest 生成的文件名里**不含 provider**
    # （是 text 哈希 + 序号），所以仍然是双盲；端点层不再替换。
    failed = result.get("failed") or []
    return {
        "blind_test_id": blind_id,
        "samples": [{"key": k, "audio_url": v} for k, v in sorted(samples.items())],
        "note": "provider 顺序已随机隐藏，评测员对每个样本打 1-5 听感分后 submit",
        # 合成失败的 provider 要告诉前端，否则运营会以为是系统 bug
        "failed_providers": failed,
    }


class BlindTestScoreItem(BaseModel):
    sample_key: str
    score: float = Field(ge=1, le=5)


class BlindTestSubmitRequest(BaseModel):
    evaluator_id: str = Field(min_length=1, max_length=64)
    scores: list[BlindTestScoreItem] = Field(min_length=1, max_length=20)


@router.post("/api/v1/admin/tts/blind-test/{blind_id}/submit")
async def admin_tts_blind_test_submit(
    blind_id: str,
    req: BlindTestSubmitRequest,
    user: dict = Depends(require_admin_or_operator),
):
    """评测员提交一份盲测打分（按 evaluator_id 覆盖式更新）。"""
    _blind_gc()
    session = _blind_test_sessions.get(blind_id)
    if session is None:
        raise NotFound(f"盲测 {blind_id} 不存在或已过期（24h TTL）")
    valid_keys = set(session["samples"])
    for item in req.scores:
        if item.sample_key not in valid_keys:
            raise InvalidRequest(f"sample_key {item.sample_key} 不属于盲测 {blind_id}")
    # 记录 per-evaluator：存 evaluator -> {sample: score}，聚合时转 sample -> [scores]
    session.setdefault("by_evaluator", {})[req.evaluator_id] = {
        i.sample_key: i.score for i in req.scores
    }
    return {"blind_test_id": blind_id, "evaluator_id": req.evaluator_id, "accepted": True}


@router.get("/api/v1/admin/tts/blind-test/{blind_id}/results")
async def admin_tts_blind_test_results(
    blind_id: str,
    user: dict = Depends(require_admin_or_operator),
):
    """盲测聚合：每个 provider 的中位分（compute_blind_score）。"""
    _blind_gc()
    session = _blind_test_sessions.get(blind_id)
    if session is None:
        raise NotFound(f"盲测 {blind_id} 不存在或已过期（24h TTL）")

    from distill.tts_blind_test import TtsBlindTest

    # provider_mapping：sample_{i+1} -> providers[i]（setup 保存的原始列表）
    provider_mapping = {
        f"sample_{i + 1}": session["providers"][i] for i in range(len(session["providers"]))
    }
    sample_scores: dict[str, list[float]] = {}
    for per_sample in session.get("by_evaluator", {}).values():
        for key, score in per_sample.items():
            sample_scores.setdefault(key, []).append(score)

    result = TtsBlindTest().compute_blind_score(sample_scores, provider_mapping)
    return {
        "blind_test_id": blind_id,
        "evaluator_count": len(session.get("by_evaluator", {})),
        "provider_median": result,
        "revealed_mapping": provider_mapping,  # 结果揭晓时才暴露映射
    }
