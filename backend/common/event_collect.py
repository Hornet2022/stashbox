"""客户端事件收集端点 SDK 适配层（CP6.2.2.1）。"""

import time
from typing import Any, Dict, List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from stashbox.backend.common.auth import require_user_optional
from stashbox.backend.common.events import EventName
from stashbox.backend.common.analytics import track
from stashbox.backend.common.database import AsyncSessionLocal

router = APIRouter(prefix="/api/v1/events", tags=["events"])

# 内存限流（每设备 100 events/min）
_rate_limit: Dict[str, List[float]] = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 100
# 限流字典的 key 数量上限。限流是按**客户端自报的 device_id** 算的，所以轮换
# device_id 就能把限流完全绕开；而 key 又只增不减（原实现只裁剪桶内时间戳，
# 从不删 key），一路涨到 OOM。给它上限 + 定期清扫，见 _sweep_rate_limit。
_RATE_LIMIT_MAX_KEYS = 20_000
# 每累计这么多次调用做一次清扫：把 O(n) 的扫描摊薄到单次请求上，
# 免得「限流」自己变成放大攻击的 CPU 热点。
_RATE_LIMIT_SWEEP_EVERY = 500
_sweep_counter = 0


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


def _sweep_rate_limit(now: float) -> None:
    """清掉「窗口内一个事件都没有」的桶，并给 key 数量兜个上限。

    原实现只做 `bucket[:] = [t for t in bucket if ...]` —— 裁的是**桶内的时间戳**，
    key 本身永远留着。于是每个新 device_id 都在字典里多占一个 entry，且永不释放。
    轮换 device_id 既能完全绕开限流（每个 id 各自 100/min），又能稳定地把内存
    推高 —— 两个问题同源。

    清扫分两步：
      1. 过期桶（最后一个事件也已出窗）直接删；
      2. 仍超上限时，按最后事件时间丢最老的 —— 宁可让一部分活跃设备少算限流，
         也不能让进程被 OOM 干掉（限流是为了保护后端，不是为了精确）。
    """
    stale = [k for k, v in _rate_limit.items() if not v or now - v[-1] >= _RATE_LIMIT_WINDOW]
    for k in stale:
        del _rate_limit[k]

    overflow = len(_rate_limit) - _RATE_LIMIT_MAX_KEYS
    if overflow > 0:
        oldest = sorted(_rate_limit, key=lambda k: _rate_limit[k][-1] if _rate_limit[k] else 0.0)
        for k in oldest[:overflow]:
            del _rate_limit[k]


def _check_rate_limit(device_id: str) -> None:
    """内存限流：100 events / 60s / device_id。"""
    global _sweep_counter

    now = time.time()
    bucket = _rate_limit.setdefault(device_id, [])
    bucket[:] = [t for t in bucket if now - t < _RATE_LIMIT_WINDOW]
    if len(bucket) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    bucket.append(now)

    _sweep_counter += 1
    if _sweep_counter >= _RATE_LIMIT_SWEEP_EVERY:
        _sweep_counter = 0
        _sweep_rate_limit(now)


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
async def collect_event(
    req: EventCollectRequest,
    user: Optional[dict] = Depends(require_user_optional),
) -> Dict[str, Any]:
    """客户端 SDK 上报事件端点（CP6.2.2.1 最小化版）。

    协议：device_id 必填 + event_name 在 EventName 枚举内。
    落库：直接走 feedback 表（CP6.2.1 已建）。

    ⚠️ **user_id 一律从 token 取，绝不采信请求体**（2026-10 修）：

    原来直接 `user_id=req.user_id or 0`，等于「谁都能以任意用户的名义写埋点」。
    而 `feedback.user_id` 的外键已在 `alembic/versions/0018_drop_feedback_user_fk.py`
    被删掉，模型里只是裸 BigInteger —— 任意 user_id 都写得进去。被污染的行会流进
    运营看板和 `content-service/admin_router.py` 的反馈 CSV 导出。

    「device_id 必填」的设计本意是允许未登录设备上报，所以**匿名上报要继续可用**
    （没有 token → user_id=0，与 analytics.track 的约定一致）。改变的只是：
    带 token 时以 token 为准、带别人的 user_id 也不行。
    """
    _check_rate_limit(req.device_id)
    event = _validate_event_name(req.event_name)

    # token 里的身份 > 请求体里的自报身份
    resolved_user_id = int(user["sub"]) if user else 0

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
            user_id=resolved_user_id,
            article_id=str(req.article_id) if req.article_id else "0",
            metadata=properties_with_meta,
        )
        if record_id is None:
            # track 内部已 rollback，事件没落库 —— 不许谎报 ok=True
            return {"ok": False, "event_id": None}
        await session.commit()

    return {"ok": True, "event_id": record_id}
