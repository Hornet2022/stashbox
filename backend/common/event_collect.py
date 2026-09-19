"""客户端事件收集端点 SDK 适配层（CP6.2.2.1）。"""

import time
from typing import Any, Dict, List, Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from stashbox.backend.common.events import EventName
from stashbox.backend.common.analytics import track
from stashbox.backend.common.database import AsyncSessionLocal

router = APIRouter(prefix="/api/v1/events", tags=["events"])

# 内存限流（每设备 100 events/min）
_rate_limit: Dict[str, List[float]] = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 100


class EventCollectRequest(BaseModel):
    """客户端事件收集请求体（v1 §11.6 CP6.2 SDK 协议）。

    注意：task §4.3 schema 与 analytics.track / Feedback 存在字段类型冲突。
    冲突处理（见 known issues）：
    - article_id: task schema=Optional[int]，track/Feedback=Mapped[str] → 按 task schema 建，
      传 track 时转 str（int 不会损失精度）
    - user_id: task schema=Optional[int]，track=必填 int → 按 task schema 建，
      传 track 时 0 替代 None（Feedback.user_id 非空）
    - device_id/client_ts: 按 task §4.3 末尾推荐方案，塞 properties dict，不动 schema
    """

    event_name: str = Field(..., min_length=1, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=128)
    user_id: Optional[int] = None
    article_id: Optional[int] = None
    properties: Dict[str, Any] = Field(default_factory=dict)
    client_ts: Optional[datetime] = None


def _check_rate_limit(device_id: str) -> None:
    """内存限流：100 events / 60s / device_id。"""
    now = time.time()
    bucket = _rate_limit.setdefault(device_id, [])
    bucket[:] = [t for t in bucket if now - t < _RATE_LIMIT_WINDOW]
    if len(bucket) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    bucket.append(now)


def _validate_event_name(name: str) -> EventName:
    """事件名必须在 EventName 枚举里。"""
    try:
        return EventName(name)
    except ValueError:
        valid = [e.value for e in EventName]
        raise HTTPException(
            status_code=400,
            detail=f"invalid event_name: {name!r}, valid: {valid[:5]}...",
        )


@router.post("/collect")
async def collect_event(req: EventCollectRequest) -> Dict[str, Any]:
    """客户端 SDK 上报事件端点（CP6.2.2.1 最小化版）。

    协议：device_id 必填 + event_name 在 EventName 枚举内。
    落库：直接走 feedback 表（CP6.2.1 已建）。
    """
    _check_rate_limit(req.device_id)
    event = _validate_event_name(req.event_name)

    # device_id + client_ts 按 task §4.3 推荐方案塞 properties（不动 feedback schema）
    properties_with_meta = {
        **req.properties,
        "device_id": req.device_id,
        "client_ts": req.client_ts.isoformat() if req.client_ts else None,
    }

    # 本端点没有 Depends(get_db) 注入的请求级事务，自己开一个 session。
    # 注意（CP7.3-audit-fix-2）：track() 只 flush 不 commit（见 analytics.py），
    # 而 `async with AsyncSessionLocal() as session` 退出时只 close、不 commit
    # （database.get_db() 同样只在 finally 里 close），未提交的事务会被回滚 ——
    # 原来这里没有 commit，导致客户端 SDK 上报的事件 100% 静默丢失。
    async with AsyncSessionLocal() as session:
        record_id = await track(
            session,
            event,
            user_id=req.user_id or 0,
            article_id=str(req.article_id) if req.article_id else "0",
            metadata=properties_with_meta,
        )
        if record_id is None:
            # track 内部已 rollback，事件没落库 —— 不许谎报 ok=True
            return {"ok": False, "event_id": None}
        await session.commit()

    return {"ok": True, "event_id": record_id}
