"""埋点 SDK（CP6.2.1, v1 §11.6）。

设计：
- `track(event_name, user_id, article_id, ...)` 异步写 feedback 表
- 失败不抛异常（埋点失败不能拖垮业务）
- 50 事件枚举在 events.py，SDK 不限定事件名
- 单测覆盖 happy path + failure tolerance
"""
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .events import EventName
from .models import Feedback

log = logging.getLogger(__name__)


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
        db: AsyncSession（调用方传入，失败时 SDK 自己处理）
        event: EventName 或 str（CP6.2.2 接入时用 str 兼容外部事件名）
        user_id: 用户 ID
        article_id: 文章 ID
        rating: 评分（仅 rate 类型）
        reason: 原因（仅 skip 类型）
        metadata: 客户端埋点的上下文 JSON
    """
    event_name = event.value if isinstance(event, EventName) else event

    try:
        record = Feedback(
            user_id=user_id,
            article_id=article_id,
            type=event_name,
            rating=rating,
            reason=reason,
            metadata_=metadata or {},
        )
        db.add(record)
        await db.flush()  # 不 commit —— 让调用方的事务统一管
        return record.id
    except Exception as exc:
        # 埋点失败不能让业务挂掉
        log.warning(f"埋点失败（忽略）: event={event_name} user={user_id} article={article_id} err={exc}")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def track_simple(
    db: AsyncSession,
    event: EventName | str,
    user_id: int,
    article_id: str,
) -> int | None:
    """简化版 track（CP6.2.1 默认风格）。"""
    return await track(db, event, user_id=user_id, article_id=article_id)
