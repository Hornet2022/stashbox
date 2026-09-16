"""
配额扣减事务 - 乐观锁（v1 §4.10）。

规则：
  - 扣减：UPDATE users SET quota_used=quota_used+1, quota_version=quota_version+1
          WHERE id=:uid AND quota_version=:expected AND quota_used < monthly_quota
  - 版本号用 `select(...).with_for_update()` 读取（见约束：SQLAlchemy 2.0 async ORM，不写 raw SQL）
  - 冲突（rowcount=0）→ 重新读版本号重试，最多 3 次
  - 配额用尽 → QuotaExceededError（code 3001 / HTTP 403）
  - 蒸馏失败 → 退还（quota_used-1, quota_version+1）
  - 每月 1 号 00:00 重置（CP1.6 用简单 asyncio 定时器，CP7 再上 apscheduler）
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common import cache_service
from stashbox.backend.common.exceptions import BizException
from stashbox.backend.common.models import User

MAX_RETRY = 3


class QuotaExceededError(BizException):
    """配额用尽（v1 §3.0 错误码 3001）。"""

    code = 3001
    message = "Quota exceeded"
    http_status = 403


class QuotaConflictError(BizException):
    """乐观锁重试 3 次仍冲突。"""

    code = 3002
    message = "Quota update conflict, please retry"
    http_status = 409


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_month_start(now: datetime) -> datetime:
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


async def _load_version(session: AsyncSession, user_id: int) -> tuple[int, int, int]:
    """读取 (quota_version, quota_used, monthly_quota)。"""
    result = await session.execute(
        select(User.quota_version, User.quota_used, User.monthly_quota).where(
            User.id == user_id
        ).with_for_update()
    )
    row = result.one_or_none()
    if row is None:
        raise BizException(message=f"user {user_id} not found", code=40400, http_status=404)
    return int(row[0]), int(row[1]), int(row[2])


async def _apply(
    session: AsyncSession, user_id: int, delta: int
) -> dict:
    """执行一次带版本号的 delta 变更（+1 扣减 / -1 退还），带 3 次重试。"""
    for attempt in range(MAX_RETRY):
        if attempt > 0:
            await session.rollback()  # 释放上一次 select ... for update 的行锁

        version, used, monthly = await _load_version(session, user_id)
        if delta > 0 and used + delta > monthly:
            await session.rollback()
            raise QuotaExceededError(message=f"quota exceeded: {used}/{monthly}")
        if delta < 0 and used + delta < 0:
            await session.rollback()
            raise BizException(message="quota_used already 0", code=3003, http_status=400)

        stmt = update(User).where(
            User.id == user_id,
            User.quota_version == version,
        )
        if delta > 0:
            stmt = stmt.where(User.quota_used < User.monthly_quota)  # v1 §4.10
        result = await session.execute(
            stmt.values(
                quota_used=User.quota_used + delta,
                quota_version=User.quota_version + 1,
            )
        )
        if result.rowcount == 0:
            # 版本冲突：重试前重新读 quota_version（下一轮循环开头 rollback + re-select）
            continue
        await session.commit()

        new_version = version + 1
        # 缓存失效：Lua 原子（DEL + 写版本号栅栏），防止旧值回填
        await cache_service.invalidate_quota(user_id, new_version)
        return {
            "user_id": user_id,
            "quota_used": used + delta,
            "monthly_quota": monthly,
            "version": new_version,
        }

    raise QuotaConflictError(message=f"quota update conflict after {MAX_RETRY} retries")


async def consume(session: AsyncSession, user_id: int, amount: int = 1) -> dict:
    """扣减配额（配额不足抛 QuotaExceededError 3001）。"""
    return await _apply(session, user_id, amount)


async def refund(session: AsyncSession, user_id: int, amount: int = 1) -> dict:
    """退还配额（蒸馏失败时调用）。"""
    return await _apply(session, user_id, -amount)


async def get_quota(session: AsyncSession, user_id: int) -> dict:
    """读配额：先 Redis，miss 则查 DB + 回填。"""
    cached = await cache_service.get_quota(user_id)
    if cached:
        cached["cached"] = True
        return cached

    result = await session.execute(
        select(
            User.quota_used, User.monthly_quota, User.quota_version, User.quota_reset_at
        ).where(User.id == user_id)
    )
    row = result.one_or_none()
    if row is None:
        raise BizException(message=f"user {user_id} not found", code=40400, http_status=404)

    payload = {
        "quota_used": int(row[0]),
        "monthly_quota": int(row[1]),
        "version": int(row[2]),
        "reset_at": row[3].isoformat() if row[3] else _next_month_start(_now()).isoformat(),
        "remaining": int(row[1]) - int(row[0]),
        "cached": False,
    }
    await cache_service.set_quota(user_id, payload)
    return payload


async def reset_monthly(session: AsyncSession) -> int:
    """月度重置：quota_used=0, quota_version+1, quota_reset_at=下月 1 号。"""
    result = await session.execute(
        update(User)
        .where(User.quota_used != 0)
        .values(
            quota_used=0,
            quota_version=User.quota_version + 1,
            quota_reset_at=_next_month_start(_now()),
        )
        .returning(User.id, User.quota_version)
    )
    rows = result.all()
    await session.commit()
    for uid, version in rows:
        await cache_service.invalidate_quota(int(uid), int(version))
    return len(rows)


async def quota_reset_loop(interval_sec: int = 3600) -> None:
    """简单定时器（CP1.6）：每小时检查一次，跨月则重置。

    CP7 再替换为 apscheduler。
    """
    from stashbox.backend.common.database import AsyncSessionLocal

    while True:
        try:
            now = _now()
            async with AsyncSessionLocal() as session:
                users = (
                    await session.execute(
                        select(User.id).where(
                            (User.quota_reset_at.is_(None))
                            | (User.quota_reset_at <= now)
                        )
                    )
                ).scalars().all()
                if users:
                    await reset_monthly(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # 定时器不能拖垮服务
            pass
        await asyncio.sleep(interval_sec)


def next_reset_at() -> datetime:
    """给 API 展示用：下月 1 号 00:00（UTC）。"""
    return _next_month_start(_now()).replace(tzinfo=timezone.utc)
