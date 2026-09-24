"""CP5.7.0 §3.1 决策 4：few-shot 池健康度监测。

按 docs/听感产品化方案_v1.md §3.1 决策 4 严格实现：
- 池子健康度：高/中/低分分布 + 活跃率
- 健康分公式：高分占比 * 70 + 活跃率 * 30 (0-100)
- 警告：低质量 / 过期 / 不足

依赖：CP3.7.1 FewShotExample ORM
"""

from __future__ import annotations

import structlog
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import FewShotExample

if TYPE_CHECKING:
    from .schemas import PoolHealthReport

log = structlog.get_logger("distill.pool_health")

# 健康分阈值
HEALTH_SCORE_GOOD = 70
HEALTH_SCORE_FAIR = 40

# 警告标签
WARNING_LOW_QUALITY = "low_quality"
WARNING_STALE = "stale"
WARNING_INSUFFICIENT = "insufficient"

# 高分阈值（与 few_shot_pool 入选门槛一致）
HIGH_SCORE_THRESHOLD = 4
LOW_SCORE_THRESHOLD = 3

# 过期天数
STALE_DAYS = 30

# 池子最小规模
MIN_POOL_SIZE = 100


class PoolHealthMonitor:
    """CP5.7.0 §3.1：池子健康度监测。"""

    async def compute_health(
        self,
        db: AsyncSession,
    ) -> "PoolHealthReport":
        """CP5.7.0 §3.1：算池子健康度。

        失败兜底：异常 → 返空报告（默认 health_score=0）。
        """
        from .schemas import PoolHealthReport

        try:
            # 总数
            total_count = await db.scalar(select(func.count()).select_from(FewShotExample)) or 0

            # 高/中/低分
            high_count = (
                await db.scalar(
                    select(func.count())
                    .select_from(FewShotExample)
                    .where(FewShotExample.score_avg >= HIGH_SCORE_THRESHOLD)
                )
                or 0
            )
            medium_count = (
                await db.scalar(
                    select(func.count())
                    .select_from(FewShotExample)
                    .where(
                        FewShotExample.score_avg >= LOW_SCORE_THRESHOLD,
                        FewShotExample.score_avg < HIGH_SCORE_THRESHOLD,
                    )
                )
                or 0
            )
            low_count = (
                await db.scalar(
                    select(func.count())
                    .select_from(FewShotExample)
                    .where(FewShotExample.score_avg < LOW_SCORE_THRESHOLD)
                )
                or 0
            )

            # 活跃 + 过期
            active_count = (
                await db.scalar(
                    select(func.count())
                    .select_from(FewShotExample)
                    .where(FewShotExample.active == True)  # noqa: E712
                )
                or 0
            )

            stale_cutoff = datetime.now() - timedelta(days=STALE_DAYS)
            stale_count = (
                await db.scalar(
                    select(func.count())
                    .select_from(FewShotExample)
                    .where(
                        (
                            FewShotExample.last_used_at.is_(None)
                            | (FewShotExample.last_used_at < stale_cutoff)
                        ),
                        FewShotExample.active == True,  # noqa: E712
                    )
                )
                or 0
            )

            # 健康分
            health_score = self.compute_health_score(
                high_score_count=high_count,
                total_count=total_count,
                stale_count=stale_count,
            )

            # 警告
            warning: Optional[str] = None
            if total_count < MIN_POOL_SIZE:
                warning = WARNING_INSUFFICIENT
            elif health_score < HEALTH_SCORE_FAIR:
                warning = WARNING_LOW_QUALITY
            elif stale_count > total_count * 0.5:
                warning = WARNING_STALE

            log.info(
                "pool_health_computed",
                total=total_count,
                high=high_count,
                medium=medium_count,
                low=low_count,
                stale=stale_count,
                health_score=health_score,
                warning=warning,
            )
            return PoolHealthReport(
                total_count=total_count,
                high_score_count=high_count,
                medium_score_count=medium_count,
                low_score_count=low_count,
                active_count=active_count,
                stale_count=stale_count,
                health_score=health_score,
                warning=warning,
            )
        except Exception as e:
            log.warning("pool_health_compute_failed_fallback", error=str(e))
            from .schemas import PoolHealthReport

            return PoolHealthReport(
                total_count=0,
                high_score_count=0,
                medium_score_count=0,
                low_score_count=0,
                active_count=0,
                stale_count=0,
                health_score=0.0,
                warning=WARNING_INSUFFICIENT,
            )

    def compute_health_score(
        self,
        high_score_count: int,
        total_count: int,
        stale_count: int,
    ) -> float:
        """CP5.7.0：算综合健康分（0-100）。

        公式：
        - 高分占比 (high/total) * 70 (主权重)
        - 活跃率 (1 - stale/total) * 30 (辅权重)
        """
        if total_count == 0:
            return 0.0
        high_ratio = high_score_count / total_count
        active_ratio = max(0.0, 1.0 - stale_count / total_count)
        return round(high_ratio * 70 + active_ratio * 30, 2)
