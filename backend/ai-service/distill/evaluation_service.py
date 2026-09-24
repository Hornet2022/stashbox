"""B1 / G1：用户 4 维评分提交服务（Android 评分 UI → distillation_evaluations）。

设计（docs/补齐方案_接口端点缺口_v1.md §3-B1）：
- 写 evaluation（auto_flag=false 用户来源）
- 同请求联动：
  * overall >= 4 → add_high_score_to_pool（门槛与 hook 链路一致，函数内置拒绝）
  * → update_user_listening_pattern（30 篇窗口 + 冷启动保护内置）
- 联动失败只 log + 返回标志 false，**不吞评分写入**
- 校验口径对齐 /rate：手动 400（code 4001），不走 FastAPI 422
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.exceptions import InvalidRequest
from stashbox.backend.common.models import DistillationEvaluation, DistilledArticle

from .few_shot_pool import add_high_score_to_pool
from .listening_pattern_updater import update_user_listening_pattern

log = structlog.get_logger("distill.evaluation_service")

_DIM_FIELDS = ("hook_score", "section_score", "outro_score", "rhythm_score")


def validate_score(value: Any, name: str, required: bool = False) -> None:
    """1-5 整数校验；None 允许（除非 required）。失败抛业务 400。"""
    if value is None:
        if required:
            raise InvalidRequest(message=f"{name} is required", code=4001)
        return
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise InvalidRequest(message=f"{name} must be between 1 and 5", code=4001)


def validate_evaluation_payload(req: Any) -> None:
    """整包校验：overall 必填 + 4 维可选 + skip_reason ≤32 列宽。"""
    validate_score(getattr(req, "overall_score", None), "overall_score", required=True)
    for fname in _DIM_FIELDS:
        validate_score(getattr(req, fname, None), fname)
    sr = getattr(req, "skip_reason", None)
    if sr is not None and len(sr) > 32:
        raise InvalidRequest(message="skip_reason too long (max 32 chars)", code=4001)


async def submit_user_evaluation(
    db: AsyncSession,
    da: DistilledArticle,
    user_id: int,
    *,
    hook_score: Optional[int] = None,
    section_score: Optional[int] = None,
    outro_score: Optional[int] = None,
    rhythm_score: Optional[int] = None,
    overall_score: int,
    comment: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> dict:
    """写评分 + 池/画像联动。返回响应 dict（含联动结果标志）。

    调用方（端点）负责：payload 校验、归属校验、db.commit()。
    本函数 add + flush（取 id），联动异常内部吞并 log。
    """
    now = datetime.now()
    evaluation = DistillationEvaluation(
        id=f"eval_{uuid.uuid4().hex[:24]}",
        task_id=da.id,
        user_id=user_id,
        hook_score=hook_score,
        section_score=section_score,
        outro_score=outro_score,
        rhythm_score=rhythm_score,
        overall_score=overall_score,
        comment=comment,
        skip_reason=skip_reason,
        auto_flag=False,
        created_at=now,
        updated_at=now,
    )
    db.add(evaluation)
    await db.flush()

    # -- 联动 1：高分入 few-shot 池（script_text 首段=hook；<4 函数内置拒绝） --
    in_pool = False
    try:
        hook_text = (da.script_text or "").split("\n\n", 1)[0].strip()
        if hook_text:
            pool_row = await add_high_score_to_pool(
                db, evaluation, hook_text, "hook", user_id=user_id
            )
            in_pool = pool_row is not None
    except Exception as e:  # noqa: BLE001 —— 联动失败不破评分写入
        log.warning("eval_pool_link_failed", task_id=da.id, error=str(e))

    # -- 联动 2：画像增量更新（冷启动 <5 保护内置，返回 None 属正常） --
    pattern_updated = False
    try:
        pat = await update_user_listening_pattern(db, user_id, evaluation)
        pattern_updated = pat is not None
    except Exception as e:  # noqa: BLE001
        log.warning("eval_pattern_link_failed", task_id=da.id, error=str(e))

    log.info(
        "user_evaluation_submitted",
        eval_id=evaluation.id,
        task_id=da.id,
        user_id=user_id,
        overall=overall_score,
        in_pool=in_pool,
        pattern_updated=pattern_updated,
    )
    return {
        "id": evaluation.id,
        "task_id": da.id,
        "overall_score": overall_score,
        "in_few_shot_pool": in_pool,
        "pattern_updated": pattern_updated,
    }
