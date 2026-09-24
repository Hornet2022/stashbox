"""CP5.7.0 §3.1 决策 4：池子人工抽查（运营评分）。

按 docs/听感产品化方案_v1.md §3.1 决策 4 严格实现：
- select_for_audit: 选池子样本（50% 高 + 30% 中 + 20% 低）
- record_audit_result: 加权平均更新 score_avg

依赖：CP3.7.1 FewShotExample ORM
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import FewShotExample

log = structlog.get_logger("distill.pool_audit")

# 抽查样本比例
HIGH_RATIO = 0.5
MEDIUM_RATIO = 0.3
LOW_RATIO = 0.2

# 评分阈值（与 pool_health 一致）
HIGH_SCORE_THRESHOLD = 4
LOW_SCORE_THRESHOLD = 3


class PoolAuditService:
    """CP5.7.0 §3.1：池子人工抽查。"""

    async def select_for_audit(
        self,
        db: AsyncSession,
        sample_size: int = 10,
    ) -> list[FewShotExample]:
        """CP5.7.0：选池子样本供人工抽查。

        策略：
        - 50% 高分（>= 4）：验证质量
        - 30% 中分（3-4）：验证边界
        - 20% 低分（< 3）：验证是否误判

        失败兑底：异常 → return []
        """
        try:
            high_n = max(1, int(sample_size * HIGH_RATIO))
            medium_n = max(1, int(sample_size * MEDIUM_RATIO))
            low_n = max(0, sample_size - high_n - medium_n)

            samples: list[FewShotExample] = []

            # 高分
            high_result = await db.execute(
                select(FewShotExample)
                .where(
                    FewShotExample.score_avg >= HIGH_SCORE_THRESHOLD,
                    FewShotExample.active == True,  # noqa: E712
                )
                .order_by(FewShotExample.score_avg.desc())
                .limit(high_n)
            )
            samples.extend(high_result.scalars().all())

            # 中分
            medium_result = await db.execute(
                select(FewShotExample)
                .where(
                    FewShotExample.score_avg >= LOW_SCORE_THRESHOLD,
                    FewShotExample.score_avg < HIGH_SCORE_THRESHOLD,
                    FewShotExample.active == True,  # noqa: E712
                )
                .order_by(FewShotExample.score_avg.desc())
                .limit(medium_n)
            )
            samples.extend(medium_result.scalars().all())

            # 低分
            low_result = await db.execute(
                select(FewShotExample)
                .where(
                    FewShotExample.score_avg < LOW_SCORE_THRESHOLD,
                    FewShotExample.active == True,  # noqa: E712
                )
                .order_by(FewShotExample.score_avg.asc())
                .limit(low_n)
            )
            samples.extend(low_result.scalars().all())

            log.info(
                "pool_audit_selected",
                sample_size=sample_size,
                selected=len(samples),
                high=high_n,
                medium=medium_n,
                low=low_n,
            )
            return samples
        except Exception as e:
            log.warning("pool_audit_select_failed", error=str(e))
            return []

    async def record_audit_result(
        self,
        db: AsyncSession,
        example_id: str,
        audit_score: float,
        auditor_id: str,
    ) -> bool:
        """CP5.7.0：记录人工抽查结果（更新 score_avg 加权平均）。

        算法：new_score_avg = (old_avg * usage_count + audit_score) / (usage_count + 1)
        """
        try:
            ex = await db.scalar(select(FewShotExample).where(FewShotExample.id == example_id))
            if ex is None:
                log.warning(
                    "pool_audit_record_example_not_found",
                    example_id=example_id,
                )
                return False

            old_avg = float(ex.score_avg or 0)
            usage_count = int(ex.usage_count or 0)
            new_avg = (old_avg * usage_count + audit_score) / (usage_count + 1)
            ex.score_avg = new_avg
            ex.usage_count = usage_count + 1
            # last_used_at 由 stage_cache / pool_health 自然更新

            await db.commit()
            log.info(
                "pool_audit_recorded",
                example_id=example_id,
                auditor=auditor_id,
                old_avg=old_avg,
                audit_score=audit_score,
                new_avg=new_avg,
            )
            return True
        except Exception as e:
            log.warning(
                "pool_audit_record_failed",
                example_id=example_id,
                error=str(e),
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return False
