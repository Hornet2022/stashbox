"""CP5.7.0 §3.1 决策 4：池子清理（长期不用 + 低分 + 重复）。

按 docs/听感产品化方案_v1.md §3.1 决策 4 严格实现：
- cleanup_stale: 30 天未使用的 few-shot 删除
- cleanup_low_quality: 评分 < 2.5 的低分文本删除
- cleanup_duplicates: 同 source_pattern 重复按 usage_count 保留最高分
- run_full_cleanup: 整合 3 pass
"""

from __future__ import annotations

import structlog
from datetime import datetime, timedelta
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import FewShotExample

log = structlog.get_logger("distill.pool_cleanup")

# 过期天数
STALE_DAYS_DEFAULT = 30

# 低分阈值
LOW_SCORE_THRESHOLD = 2.5


class PoolCleanupService:
    """CP5.7.0 §3.1：池子清理。"""

    async def cleanup_stale(
        self,
        db: AsyncSession,
        days: int = STALE_DAYS_DEFAULT,
    ) -> int:
        """CP5.7.0：清理 30 天未使用的 few-shot。

        Returns:
            删除数量
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)
            result = await db.execute(
                delete(FewShotExample).where(
                    (FewShotExample.last_used_at.is_(None) | (FewShotExample.last_used_at < cutoff))
                )
            )
            deleted = result.rowcount or 0
            await db.commit()
            log.info(
                "pool_cleanup_stale",
                days=days,
                deleted=deleted,
            )
            return deleted
        except Exception as e:
            log.warning("pool_cleanup_stale_failed", error=str(e))
            try:
                await db.rollback()
            except Exception:
                pass
            return 0

    async def cleanup_low_quality(
        self,
        db: AsyncSession,
        score_threshold: float = LOW_SCORE_THRESHOLD,
    ) -> int:
        """CP5.7.0：清理评分 < 2.5 的低分文本。

        Returns:
            删除数量
        """
        try:
            result = await db.execute(
                delete(FewShotExample).where(FewShotExample.score_avg < score_threshold)
            )
            deleted = result.rowcount or 0
            await db.commit()
            log.info(
                "pool_cleanup_low_quality",
                threshold=score_threshold,
                deleted=deleted,
            )
            return deleted
        except Exception as e:
            log.warning("pool_cleanup_low_quality_failed", error=str(e))
            try:
                await db.rollback()
            except Exception:
                pass
            return 0

    async def cleanup_duplicates(
        self,
        db: AsyncSession,
    ) -> int:
        """CP5.7.0：清理同 source_pattern 重复（按 score_avg 保留最高分）。

        Returns:
            删除数量
        """
        try:
            # 找出有重复的 source_pattern
            duplicates_result = await db.execute(
                select(FewShotExample).order_by(
                    FewShotExample.source_pattern,
                    FewShotExample.score_avg.desc(),
                )
            )
            all_examples = duplicates_result.scalars().all()

            seen_patterns: dict[str, str] = {}  # pattern -> id to keep
            to_delete: list[str] = []

            for ex in all_examples:
                if ex.source_pattern in seen_patterns:
                    to_delete.append(ex.id)
                else:
                    seen_patterns[ex.source_pattern] = ex.id

            if to_delete:
                await db.execute(delete(FewShotExample).where(FewShotExample.id.in_(to_delete)))
                await db.commit()

            log.info(
                "pool_cleanup_duplicates",
                deleted=len(to_delete),
            )
            return len(to_delete)
        except Exception as e:
            log.warning("pool_cleanup_duplicates_failed", error=str(e))
            try:
                await db.rollback()
            except Exception:
                pass
            return 0

    async def run_full_cleanup(
        self,
        db: AsyncSession,
    ) -> dict:
        """CP5.7.0：跑完整清理（stale + low_quality + duplicates）。

        Returns:
            {"stale": N, "low_quality": N, "duplicates": N, "total": N}
        """
        stale_count = await self.cleanup_stale(db)
        low_count = await self.cleanup_low_quality(db)
        dup_count = await self.cleanup_duplicates(db)
        total = stale_count + low_count + dup_count
        log.info(
            "pool_cleanup_full",
            stale=stale_count,
            low_quality=low_count,
            duplicates=dup_count,
            total=total,
        )
        return {
            "stale": stale_count,
            "low_quality": low_count,
            "duplicates": dup_count,
            "total": total,
        }
