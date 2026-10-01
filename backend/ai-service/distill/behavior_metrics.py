"""收听行为指标（2026-10-02 新增）。

## 为什么单独一个模块

`user_listening_patterns` 表里 `skip_rate` / `completion_rate` /
`avg_session_sec` 三列**从建表至今没有任何写入方** —— 恒为 NULL。原因是它们
根本不属于「评分驱动」那条路：画像更新（listening_pattern_updater）只由
`DistillationEvaluation`（用户主动打的 4 维分）触发，而这三个指标描述的是
**收听行为**，来源是 `listening_statuses`。

结果就是方案 §1 闭环 2 的画像五特征里，三个是死字段 —— 个性化永远学不到
「这个用户听完还是早退」。

## 口径

以 `listening_statuses` 每行（用户 × 文章）为一个样本：

- **完听率**：听完的比例。`position_sec >= total_sec * 0.9` 记为听完。
  0.9 而不是 1.0：结尾常常有静默/片尾，直接卡 100% 会把「听完」判得过严。
- **跳过率**：早退的比例。`position_sec < min(30s, total_sec * 0.2)` 记为跳过。
  30 秒是下限，避免把「刚点开还没听」也算跳过。
- **平均单次收听时长**：`position_sec` 的均值。

只有 `total_sec` 非空的样本才参与完听率/跳过率判定（没分母算不了），
但**所有**样本都参与平均时长。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import ListeningStatus

log = structlog.get_logger("distill.behavior_metrics")

# 完听判定阈值：听到 90% 即视为听完
_COMPLETION_RATIO = 0.9
# 跳过判定：低于 max(30s, total*0.2) 视为早退
_SKIP_FLOOR_SEC = 30
_SKIP_RATIO = 0.2
# 样本下限：太少时不出数（一个样本算出来的"率"没有意义）
_MIN_SAMPLES = 3


class BehaviorMetrics:
    """一个用户的收听行为指标（样本不足时各字段为 None）。"""

    __slots__ = ("sample_count", "skip_rate", "completion_rate", "avg_session_sec")

    def __init__(
        self,
        sample_count: int,
        skip_rate: float | None,
        completion_rate: float | None,
        avg_session_sec: float | None,
    ) -> None:
        self.sample_count = sample_count
        self.skip_rate = skip_rate
        self.completion_rate = completion_rate
        self.avg_session_sec = avg_session_sec

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return (
            f"BehaviorMetrics(n={self.sample_count}, skip={self.skip_rate}, "
            f"done={self.completion_rate}, avg_sec={self.avg_session_sec})"
        )


async def compute_behavior_metrics(
    db: AsyncSession,
    user_id: int,
    limit: int = 200,
) -> BehaviorMetrics:
    """算某用户最近的收听行为指标。

    取最近 ``limit`` 条（按 updated_at 倒序）—— 用窗口而不是全量，
    免得老数据把新行为稀释掉。
    """
    try:
        rows = (
            await db.execute(
                select(ListeningStatus.position_sec, ListeningStatus.total_sec)
                .where(ListeningStatus.user_id == user_id)
                .order_by(ListeningStatus.updated_at.desc())
                .limit(limit)
            )
        ).all()
    except Exception as exc:
        log.warning("behavior_metrics_query_failed", user_id=user_id, error=str(exc))
        return BehaviorMetrics(0, None, None, None)

    if not rows:
        return BehaviorMetrics(0, None, None, None)

    # 平均时长：所有样本都算
    positions = [int(r[0] or 0) for r in rows]
    avg_sec = sum(positions) / len(positions)

    # 完听/跳过：必须有分母
    judged = [(int(r[0] or 0), int(r[1])) for r in rows if r[1] is not None and int(r[1]) > 0]
    if len(judged) < _MIN_SAMPLES:
        return BehaviorMetrics(len(rows), None, None, round(avg_sec, 1))

    finished = sum(1 for pos, total in judged if pos >= total * _COMPLETION_RATIO)
    skipped = sum(1 for pos, total in judged if pos < max(_SKIP_FLOOR_SEC, total * _SKIP_RATIO))

    return BehaviorMetrics(
        sample_count=len(rows),
        skip_rate=round(skipped / len(judged), 4),
        completion_rate=round(finished / len(judged), 4),
        avg_session_sec=round(avg_sec, 1),
    )


async def backfill_pattern_metrics(db: AsyncSession, user_id: int) -> BehaviorMetrics:
    """把算出来的三个指标**写回** user_listening_patterns。

    写入不是主消费路径（`load_user_profile` 会直接算，避免读到过期值），
    这份落库是为了让运营在后台看得到、也为了表不再挂着三个死字段。
    """
    from datetime import datetime

    from stashbox.backend.common.models import UserListeningPattern

    metrics = await compute_behavior_metrics(db, user_id)
    if metrics.sample_count == 0:
        return metrics

    try:
        pat = await db.scalar(
            select(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
        )
        now = datetime.now()
        if pat is None:
            pat = UserListeningPattern(
                user_id=user_id,
                feedback_count=0,
                created_at=now,
                updated_at=now,
                last_updated=now,
            )
            db.add(pat)
            await db.flush()

        pat.skip_rate = metrics.skip_rate
        pat.completion_rate = metrics.completion_rate
        pat.avg_session_sec = metrics.avg_session_sec
        await db.flush()

        log.info(
            "behavior_metrics_backfilled",
            user_id=user_id,
            sample_count=metrics.sample_count,
            skip_rate=metrics.skip_rate,
            completion_rate=metrics.completion_rate,
            avg_session_sec=metrics.avg_session_sec,
        )
    except Exception as exc:
        log.warning("behavior_metrics_backfill_failed", user_id=user_id, error=str(exc))

    return metrics
