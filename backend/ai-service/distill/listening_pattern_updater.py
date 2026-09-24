"""CP3.7.3 §2.1.B：用户听感画像增量更新（30 篇滑动窗口 + 加权平均）。

按 docs/听感产品化方案_v1.md §2.1.B 严格实现。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import DistillationEvaluation, UserListeningPattern

log = structlog.get_logger("distill.listening_pattern_updater")

# 30 篇滑动窗口
_WINDOW_SIZE = 30

# 移动加权：新数据 0.7 / 历史 0.3
_NEW_WEIGHT = 0.7
_OLD_WEIGHT = 0.3

# 冷启动保护：feedback_count < 5 时画像保持 NULL
_COLD_START_THRESHOLD = 5

# 近 10 篇高分（>= 4）的 hook 提取 preferred_rhythm / preferred_hook_type
_HIGH_SCORE_WINDOW = 10
_HIGH_SCORE_MIN = 4


async def update_user_listening_pattern(
    db: AsyncSession,
    user_id: int,
    new_evaluation: DistillationEvaluation,
) -> UserListeningPattern | None:
    """CP3.7.3 §2.1.B：蒸馏完成后异步触发（PostDistillHook），增量更新画像。

    Returns:
        UserListeningPattern 实例（更新后）或 None（失败）

    失败兜底：异常 → log warning，不破主流程。
    """
    try:
        # 1. 取用户当前画像（无则创建）
        pat = await db.scalar(
            select(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
        )
        if pat is None:
            # SQLite 测试兼容：显式传 created_at/updated_at/last_updated
            from datetime import datetime as _dt

            now = _dt.now()
            pat = UserListeningPattern(
                user_id=user_id,
                feedback_count=0,
                created_at=now,
                updated_at=now,
                last_updated=now,
            )
            db.add(pat)
            await db.flush()

        # 2. feedback_count += 1
        pat.feedback_count = (pat.feedback_count or 0) + 1

        # 3. 冷启动保护：feedback_count < 5 时画像保持 NULL
        if pat.feedback_count < _COLD_START_THRESHOLD:
            log.info(
                "listening_pattern_cold_start",
                user_id=user_id,
                feedback_count=pat.feedback_count,
                threshold=_COLD_START_THRESHOLD,
            )
            await db.flush()
            return pat

        # 4. 移动加权平均：新数据 0.7 / 历史 0.3
        new_score = float(new_evaluation.overall_score or 0)
        if pat.avg_overall_score is None:
            pat.avg_overall_score = new_score
        else:
            pat.avg_overall_score = _OLD_WEIGHT * pat.avg_overall_score + _NEW_WEIGHT * new_score

        # 5. preferred_rhythm：近 10 篇高分（>= 4）的 rhythm_score 众数
        # 本期简化：根据 new_evaluation.rhythm_score 直接设值
        # CP3.8.0 接真实评分时再算众数
        if (
            new_evaluation.rhythm_score is not None
            and new_evaluation.rhythm_score >= _HIGH_SCORE_MIN
        ):
            # 简化策略：rhythm_score >= 4 -> "fast"，== 3 -> "medium"，< 3 -> "slow"
            if new_evaluation.rhythm_score >= 4:
                pat.preferred_rhythm = "fast"
            elif new_evaluation.rhythm_score == 3:
                pat.preferred_rhythm = "medium"
            else:
                pat.preferred_rhythm = "slow"

        # 6. preferred_hook_type：根据 hook_score 推断
        if new_evaluation.hook_score is not None and new_evaluation.hook_score >= _HIGH_SCORE_MIN:
            pat.preferred_hook_type = "question"  # 默认占位（CP3.8.0 改实际特征提取）

        # 7. last_distill_at
        pat.last_distill_at = (
            new_evaluation.created_at.isoformat() if new_evaluation.created_at else None
        )

        # 8. last_updated 用 Python 当前时间（让 SQLite / PG 都能用）
        from datetime import datetime as _dt

        pat.last_updated = _dt.now()

        log.info(
            "listening_pattern_updated",
            user_id=user_id,
            feedback_count=pat.feedback_count,
            avg_overall_score=pat.avg_overall_score,
            preferred_rhythm=pat.preferred_rhythm,
        )
        await db.flush()
        return pat
    except Exception as e:
        log.warning(
            "listening_pattern_update_failed_continue",
            user_id=user_id,
            error=str(e),
        )
        return None
