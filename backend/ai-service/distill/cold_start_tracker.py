"""CP5.6.1 §3.1 决策 2：冷启动状态追踪。

按 docs/听感产品化方案_v1.md §3.1 严格实现：
- 3 状态：fresh / warming / active
- 状态定义：
  - fresh: feedback_count == 0 (新用户)
  - warming: 0 < feedback_count < 5 (冷启动中)
  - active: feedback_count >= 5 (已激活个性化)
- should_prompt_rating: 前 3 篇强 / 4-5 篇中 / 5-20 篇弱 / 20+ 无
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Literal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import UserListeningPattern

if TYPE_CHECKING:
    from .schemas import ColdStartState

log = structlog.get_logger("distill.cold_start_tracker")

# 状态分类阈值
WARMING_THRESHOLD = 5  # feedback_count >= 5 → active

# 推送强度阈值
STRONG_COUNT_MAX = 2  # 0-2 强推
MEDIUM_COUNT_MAX = 4  # 3-4 中推
WEAK_COUNT_MAX = 19  # 5-19 弱推（20+ 推 stop）


class ColdStartTracker:
    """CP5.6.1 §3.1：冷启动状态追踪。"""

    async def get_state(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> "ColdStartState":
        """查询用户冷启动状态。

        Returns:
            ColdStartState: {feedback_count, state, ratings_remaining, personalization_enabled_at}

        失败兜底：异常 → fresh 状态（0 篇反馈）。
        """
        from .schemas import ColdStartState

        try:
            pat = await db.scalar(
                select(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
            )
            feedback_count = pat.feedback_count if pat else 0

            if feedback_count == 0:
                state: Literal["fresh", "warming", "active"] = "fresh"
            elif feedback_count < WARMING_THRESHOLD:
                state = "warming"
            else:
                state = "active"

            ratings_remaining = max(0, WARMING_THRESHOLD - feedback_count)

            # 读 consent 记录看是否已激活个性化
            from stashbox.backend.common.models import ConsentRecord

            try:
                consent = await db.scalar(
                    select(ConsentRecord).where(ConsentRecord.user_id == user_id)
                )
                personalization_enabled_at = (
                    consent.updated_at
                    if consent and getattr(consent, "personalization_enabled", False)
                    else None
                )
            except Exception:
                personalization_enabled_at = None

            return ColdStartState(
                feedback_count=feedback_count,
                state=state,
                ratings_remaining_to_personalize=ratings_remaining,
                personalization_enabled_at=personalization_enabled_at,
            )
        except Exception as e:
            log.warning(
                "cold_start_tracker_get_state_failed_fallback",
                user_id=user_id,
                error=str(e),
            )
            from .schemas import ColdStartState

            return ColdStartState(
                feedback_count=0,
                state="fresh",
                ratings_remaining_to_personalize=WARMING_THRESHOLD,
                personalization_enabled_at=None,
            )

    def is_fresh(self, state: "ColdStartState") -> bool:
        return state.state == "fresh"

    def is_warming(self, state: "ColdStartState") -> bool:
        return state.state == "warming"

    def is_active(self, state: "ColdStartState") -> bool:
        return state.state == "active"

    def should_prompt_rating(
        self,
        state: "ColdStartState",
        user_action_count: int = 0,
    ) -> bool:
        """CP5.6.1 §3.1：是否应该推送评分请求。

        策略：
        - fresh + action_count <= 3: True（前 3 篇每篇都推）
        - warming + ratings_remaining > 0: True（继续推）
        - active + feedback_count < 20: True（弱推）
        - active + feedback_count >= 20: False（停止）
        """
        if state.feedback_count == 0:
            return user_action_count <= 3
        if state.state == "warming":
            return state.ratings_remaining_to_personalize > 0
        if state.state == "active":
            return state.feedback_count < 20
        return False
