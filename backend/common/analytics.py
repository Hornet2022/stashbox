"""埋点 SDK（CP6.2.1, v1 §11.6）。

设计：
- `track(event_name, user_id, article_id, ...)` 异步写 feedback 表
- 失败不抛异常（埋点失败不能拖垮业务）
- 50 事件枚举在 events.py，SDK 不限定事件名
- 单测覆盖 happy path + failure tolerance
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .events import EventName
from .models import Feedback

log = logging.getLogger(__name__)


# feedback.reason 的列宽（common/models/feedback.py 里是 String(64)）。
#
# 为什么护栏要放在 SDK 这一层，而不是让每个调用点自己记得截断：
#
# 埋点失败是**静默**的 —— track() 用 SAVEPOINT 兜住异常，只留一条 warning，
# 事件就此消失，业务照常成功。于是只要有一个调用点忘了按列宽截断，
# 那条埋点就整条没了，外部完全看不出来（数据库里查不到，告警也不会响）。
#
# 真实踩过的坑：distill_task 的 agent 路径把完整异常塞进 reason（截断到 200），
# 200 > 64 → insert 报 StringDataRightTruncationError → **整条 distill_failed
# 埋点被丢掉**，管理后台看不到任何失败原因（只在 worker 日志里留了一行 warning）。
#
# 现在 reason 统一在 SDK 层压到列宽，溢出部分挪进 metadata（JSONB 无长度上限），
# 既不丢事件，也不丢原文；调用点漏截断也伤不到数据。
_REASON_MAX_LEN = 64


def _fit_reason(reason: str | None, metadata: dict[str, Any]) -> str | None:
    """把 reason 压进列宽；溢出部分挪进 metadata['reason_full']。

    返回截断后的 reason（None 原样返回）。
    """
    if reason is None:
        return None
    if len(reason) <= _REASON_MAX_LEN:
        return reason
    metadata.setdefault("reason_full", reason)
    return reason[:_REASON_MAX_LEN]


async def track(
    db: AsyncSession,
    event: EventName | str,
    *,
    user_id: int,
    article_id: str,
    rating: int | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    """记录一个埋点事件（CP6.2.1）。

    Returns:
        feedback.id if 写库成功；None if 失败（不抛异常）

    Args:
        db: AsyncSession（调用方传入）
        event: EventName 或 str（CP6.2.2 接入时用 str 兼容外部事件名）
        user_id: 用户 ID
        article_id: 文章 ID
        rating: 评分（仅 rate 类型）
        reason: 原因（仅 skip 类型）
        metadata: 客户端埋点的上下文 JSON

    实现说明（2026-09-23 修复）：
    - 原实现 `db.add + flush` 共享调用方事务，失败时 `db.rollback()` 会把
      **整个外层事务**回滚掉 —— 调用方刚 `db.add` 还没 commit 的业务对象
      （如 _create_article 里的 Article）随之被 expunge，后续 `db.refresh(art)`
      抛 InvalidRequestError → 500（真机剪藏文档链接「服务暂不可用」的放大器）。
    - 现改为：用 `db.begin_nested()`（SAVEPOINT）隔离埋点写入，失败只回滚
      savepoint，外层事务与业务对象毫发无损；成功时 savepoint 随外层 commit 落库。
    - 若传入的 session 没有活跃事务（单测/特殊场景），退化为独立新 session 写入。
    """
    event_name = event.value if isinstance(event, EventName) else event
    # 护栏在此生效：reason 压到列宽（超长部分进 metadata），调用点不必自己记得截断。
    metadata = dict(metadata or {})
    reason = _fit_reason(reason, metadata)

    def _new_record() -> Feedback:
        return Feedback(
            user_id=user_id,
            article_id=article_id,
            type=event_name,
            rating=rating,
            reason=reason,
            metadata_=metadata or {},
        )

    try:
        # SAVEPOINT 隔离：埋点失败只回滚到本 savepoint，不污染调用方事务
        async with db.begin_nested():
            record = _new_record()
            db.add(record)
            await db.flush()
        return record.id
    except Exception as exc:
        # 埋点失败不能让业务挂掉（不回滚外层事务！）
        log.warning(
            f"埋点失败（忽略，savepoint 已回滚）: event={event_name} user={user_id} article={article_id} err={exc}"
        )
        return None


async def track_simple(
    db: AsyncSession,
    event: EventName | str,
    user_id: int,
    article_id: str,
) -> int | None:
    """简化版 track（CP6.2.1 默认风格）。"""
    return await track(db, event, user_id=user_id, article_id=article_id)
