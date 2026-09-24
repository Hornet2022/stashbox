"""CP5.6.2 §3.1 决策 3：画像权重衰减（30 天前减半）。

按 docs/听感产品化方案_v1.md §3.1 决策 3 严格实现：
- 公式：weight = 0.5 ** (days_since_feedback / 30)
- 今天反馈: weight = 1.0
- 30 天前: weight = 0.5
- 60 天前: weight = 0.25
- 90 天前: weight = 0.125

依赖：CP3.7.1 user_listening_patterns + CP5.6.1 cold_start_tracker
"""

from __future__ import annotations

import structlog
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import DistillationEvaluation, UserListeningPattern

log = structlog.get_logger("distill.pattern_decay")

# 半衰期常数（CP5.6.6.2 决策 3：30 天前反馈权重减半）
HALF_LIFE_DAYS = 30
DECAY_FACTOR = 0.5


class PatternDecayCalculator:
    """CP5.6.2 §3.1：画像权重衰减。"""

    def compute_weight(
        self,
        feedback_at: datetime,
        now: datetime | None = None,
    ) -> float:
        """CP5.6.2 §3.1：算单条反馈的衰减权重。

        公式：weight = 0.5 ** (days_since_feedback / HALF_LIFE_DAYS)

        Returns:
            0.0-1.0 之间的权重
        """
        if now is None:
            now = datetime.now()
        if feedback_at is None:
            return 1.0  # 无时间戳视为最新

        days_since = (now - feedback_at).total_seconds() / 86400.0
        # 未来时间视为最新
        if days_since < 0:
            return 1.0

        weight = DECAY_FACTOR ** (days_since / HALF_LIFE_DAYS)
        return max(0.0, min(1.0, weight))

    def compute_weighted_avg(
        self,
        feedback_list: list[tuple[float, datetime]],
        now: datetime | None = None,
    ) -> float:
        """CP5.6.2：算加权平均（按时间衰减权重）。

        Args:
            feedback_list: [(score, feedback_at), ...]
            now: 当前时间（默认 now()）

        Returns:
            加权平均（0-10）
        """
        if not feedback_list:
            return 0.0

        weighted_sum = 0.0
        total_weight = 0.0
        for score, feedback_at in feedback_list:
            weight = self.compute_weight(feedback_at, now=now)
            weighted_sum += score * weight
            total_weight += weight

        if total_weight == 0:
            return 0.0
        return weighted_sum / total_weight

    async def apply_decay_to_pattern(
        self,
        db: AsyncSession,
        user_id: int,
        evaluations: list[DistillationEvaluation],
        now: datetime | None = None,
    ) -> Optional[float]:
        """CP5.6.2 §3.1：应用衰减到用户画像（重算 avg_overall_score）。

        Returns:
            新 avg_overall_score 或 None（失败）

        失败兜底：异常 → log warning return None
        """
        try:
            if not evaluations:
                return None

            # 构建 (score, feedback_at) 列表
            feedback_list = [
                (float(ev.overall_score or 0), ev.created_at)
                for ev in evaluations
                if ev.overall_score is not None and ev.created_at is not None
            ]
            if not feedback_list:
                return None

            new_avg = self.compute_weighted_avg(feedback_list, now=now)

            # 写回 user_listening_patterns
            pat = await db.scalar(
                select(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
            )
            if pat is None:
                return None
            pat.avg_overall_score = new_avg

            from datetime import datetime as _dt

            pat.last_updated = _dt.now()

            await db.commit()
            log.info(
                "pattern_decay_applied",
                user_id=user_id,
                num_evaluations=len(feedback_list),
                new_avg=new_avg,
            )
            return new_avg
        except Exception as e:
            log.warning(
                "pattern_decay_apply_failed",
                user_id=user_id,
                error=str(e),
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return None
