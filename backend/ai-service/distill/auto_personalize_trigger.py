"""CP5.6.1 §3.1：自动触发个性化（feedback_count 跳变时）。

按 docs/听感产品化方案_v1.md §3.1 严格实现：
- 5 篇门槛：feedback_count < 5 → fresh/warming；>= 5 → active
- check_threshold: 检测刚跨过 5 篇门槛的用户
- mark_personalization_enabled: 写 consent_records.personalization_enabled
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import ConsentRecord

log = structlog.get_logger("distill.auto_personalize_trigger")

THRESHOLD = 5  # CP5.6.1 §3.1 决策 2


class AutoPersonalizeTrigger:
    """CP5.6.1 §3.1：自动触发个性化。"""

    async def check_threshold(
        self,
        db: AsyncSession,
        user_id: int,
        new_feedback_count: int,
    ) -> bool:
        """CP5.6.1 §3.1：检查是否刚跨过 5 篇门槛。

        Returns:
            True if user_id just crossed from < 5 to >= 5 (新激活)

        失败兜底：异常 → False
        """
        try:
            from stashbox.backend.common.models import UserListeningPattern

            pat = await db.scalar(
                select(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
            )
            old_count = pat.feedback_count if pat else 0

            # 跨过门槛
            if old_count < THRESHOLD <= new_feedback_count:
                log.info(
                    "auto_personalize_threshold_crossed",
                    user_id=user_id,
                    old_count=old_count,
                    new_count=new_feedback_count,
                )
                return True
            return False
        except Exception as e:
            log.warning(
                "auto_personalize_check_threshold_failed",
                user_id=user_id,
                error=str(e),
            )
            return False

    async def mark_personalization_enabled(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> bool:
        """CP5.6.1：标记用户已激活个性化（写 consent_records.personalization_enabled）。

        Returns:
            True if marked successfully
        """
        try:
            consent = await db.scalar(select(ConsentRecord).where(ConsentRecord.user_id == user_id))
            if consent is None:
                consent = ConsentRecord(
                    user_id=user_id,
                    personalization_enabled=True,
                    consent_version="v2",
                )
                db.add(consent)
            else:
                consent.personalization_enabled = True
            await db.commit()
            log.info(
                "auto_personalize_marked_enabled",
                user_id=user_id,
            )
            return True
        except Exception as e:
            log.warning(
                "auto_personalize_mark_failed",
                user_id=user_id,
                error=str(e),
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return False
