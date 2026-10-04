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
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import User
from stashbox.backend.common import quota_metrics

log = get_logger(__name__)

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
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def next_reset_at_naive() -> datetime:
    """下月 1 号 00:00（UTC，**naive**）—— 碰 `users.quota_reset_at` 唯一正确的值。

    该列是 `TIMESTAMP WITHOUT TIME ZONE`（alembic 0002），而 `_now()` 是 tz-aware。
    asyncpg 拿到 tz-aware datetime 绑到 naive 列会直接抛：

        DataError: can't subtract offset-naive and offset-aware datetimes

    所以凡是**写或比**这一列都必须用它，不要各处自己 `.replace(tzinfo=None)`。
    分散写正是它出事的根因：CP1.7.3 修了写入路径（`reset_monthly`），而
    `quota_reset_loop` 的比较路径漏了，于是整条跨月重置从来没跑起来过 ——
    而那正是这条路径存在的唯一理由（见该函数的说明）。
    """
    return _next_month_start(_now()).replace(tzinfo=None)


async def _load_version(session: AsyncSession, user_id: int) -> tuple[int, int, int]:
    """读取 (quota_version, quota_used, monthly_quota)。"""
    result = await session.execute(
        select(User.quota_version, User.quota_used, User.monthly_quota)
        .where(User.id == user_id)
        .with_for_update()
    )
    row = result.one_or_none()
    if row is None:
        raise BizException(message=f"user {user_id} not found", code=40400, http_status=404)
    return int(row[0]), int(row[1]), int(row[2])


async def _apply(session: AsyncSession, user_id: int, delta: int) -> dict:
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
    with quota_metrics.quota_consume_duration_seconds.time():
        try:
            result = await _apply(session, user_id, amount)
            user = await session.get(User, user_id)
            try:
                quota_metrics.quota_consume_total.labels(
                    plan=user.plan if user else "unknown"
                ).inc()
            except Exception:
                pass
            return result
        except QuotaExceededError:
            quota_metrics.quota_consume_blocked_total.labels(reason="exceeded").inc()
            raise
        except QuotaConflictError:
            quota_metrics.quota_consume_blocked_total.labels(reason="conflict").inc()
            raise


async def refund(
    session: AsyncSession, user_id: int, amount: int = 1, trigger: str = "distill_failed"
) -> dict:
    """退还配额（蒸馏失败时调用）。"""
    try:
        result = await _apply(session, user_id, -amount)
        try:
            quota_metrics.quota_refund_total.labels(trigger=trigger).inc()
        except Exception:
            pass
        return result
    except Exception:
        raise


async def release_refund_lock(task_id: str, *, client=None) -> None:
    """退款失败时把幂等锁 `refund:{task_id}` 删掉，让 Arq 重试还能补上这笔退款。

    为什么非有不可：两个退款调用点（`ai-service/tasks/distill_task.py` 的
    `_refund_quota_once`、`ai-service/distill/pipeline.py` 的 `_refund_quota`）都是
    「先 SETNX 落锁、再退款」。而这把锁的语义是「这笔已经退过了」——它落锁的时刻
    却**早于**退款成功的时刻。中间任何一次失败（乐观锁重试耗尽的 QuotaConflictError、
    瞬时 DB 故障）都会留下一个「声称退过款、其实没退」的锁，接下来 24h 内所有 Arq
    重试都被它挡在门外。

    后果是用户为一次失败的蒸馏付了钱，而且没有任何告警 —— 这是纯亏钱、不报错、
    事后才看得见的账。

    `client`：传入调用方落锁时已经建好的 Redis 客户端（**推荐**）。不给才自己建
    一个。复用同一个 client 不是为了省连接，而是为了让「落锁」和「还锁」走同一份
    可替换的依赖 —— 否则单测里 FakeRedis 落的锁会被真 Redis 的 DEL 落空，
    测试反而测不出东西（这正是 distill_task 抽 `_make_refund_lock_client` 的原因）。
    """
    import redis.asyncio as redis_async

    from stashbox.backend.common.redis_client import get_redis_pool

    owns_client = client is None
    try:
        c = client if client is not None else redis_async.Redis(connection_pool=get_redis_pool())
        await c.delete(f"refund:{task_id}")
        if owns_client:
            await c.aclose()
    except Exception as exc:
        # 释放失败不抛：原始的退款异常比这个更值得看，不能被它盖掉
        log.warning("quota_refund_lock_release_failed", task_id=task_id, error=str(exc))


async def get_quota(session: AsyncSession, user_id: int) -> dict:
    """读配额：先 Redis，miss 则查 DB + 回填。"""
    cached = await cache_service.get_quota(user_id)
    if cached:
        quota_metrics.quota_cache_hit_total.inc()
        cached["cached"] = True
        return cached
    quota_metrics.quota_cache_miss_total.inc()
    result = await session.execute(
        select(User.quota_used, User.monthly_quota, User.quota_version, User.quota_reset_at).where(
            User.id == user_id
        )
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


async def _finish_reset(session: AsyncSession, result) -> int:
    """重置 UPDATE 的收尾：提交 + 逐个失效配额缓存 + 打点。返回被重置人数。"""
    rows = result.all()
    await session.commit()
    for uid, version in rows:
        await cache_service.invalidate_quota(int(uid), int(version))
    if rows:
        try:
            quota_metrics.quota_reset_total.inc(len(rows))
        except Exception:
            pass
    return len(rows)


def _due_filter(now_naive: datetime):
    """到期判定：没排期过（NULL）或已经过期。"""
    return (User.quota_reset_at.is_(None)) | (User.quota_reset_at <= now_naive)


async def reset_monthly(session: AsyncSession) -> int:
    """**全平台**月度重置：quota_used=0, quota_version+1, quota_reset_at=下月 1 号。

    ⚠️ 无 `User.id` 过滤 —— 这是全库操作，只有 admin 手动端点该调
    （`POST /api/v1/users/me/quota/reset-monthly`，名字里的 me 是历史遗留）。
    定时器要走范围版 `reset_due_users`，别用这个。
    """
    result = await session.execute(
        update(User)
        .where(User.quota_used != 0)
        .values(
            quota_used=0,
            quota_version=User.quota_version + 1,
            quota_reset_at=next_reset_at_naive(),
        )
        .returning(User.id, User.quota_version)
    )
    return await _finish_reset(session, result)


async def reset_due_users(session: AsyncSession) -> int:
    """重置**所有已到期**的用户 —— 定时器每一拍走这里。

    为什么把判定条件直接写进 UPDATE 的 WHERE，而不是「先 SELECT 出 id 列表，
    再 `WHERE id IN (...)`」：

      1. **长度**：跨月那一刻全平台用户同时到期，id 列表长度约等于整张 users 表。
         `IN` 几千个 id 会生成一条巨型 SQL，PG 的绑定参数上限是 65535 ——
         用户量涨上去，这条路会直接失败。而定时器恰恰是在**最不该失败的时刻**
         （月初恢复计费）失败。
      2. **窗口**：SELECT 与 UPDATE 之间存在 TOCTOU：期间新注册的用户会落进
         「被查出来要重置」和「实际被重置」之间的缝里。写进 WHERE 就没有这个缝。

    范围判据和到期判定是同一个表达式（`_due_filter`），所以「谁被重置」这件事
    不存在两处定义 —— 之前「判定在哪、重置多少」分写两处，正是 scope 丢失的根。
    """
    now_naive = _now().replace(tzinfo=None)
    result = await session.execute(
        update(User)
        .where(User.quota_used != 0)
        .where(_due_filter(now_naive))
        .values(
            quota_used=0,
            quota_version=User.quota_version + 1,
            quota_reset_at=next_reset_at_naive(),
        )
        .returning(User.id, User.quota_version)
    )
    return await _finish_reset(session, result)


async def _reset_due_users(session: AsyncSession) -> int:
    """跑一拍 = 一次 `reset_due_users`。

    单独包一层是为了让单测能直接测这一拍：外层 `quota_reset_loop` 是 `while True`
    死循环，值得钉住的是这一拍的语义（按什么条件、范围多大），不是 sleep 几秒。
    """
    return await reset_due_users(session)


async def quota_reset_loop(interval_sec: int = 3600, session_factory=None) -> None:
    """简单定时器（CP1.6）：每小时检查一次，跨月则重置。

    CP7 再替换为 apscheduler。

    session_factory：可选注入独立连接池（CP3.6.3）。默认走全局
    AsyncSessionLocal，保持历史调用行为不变。

    ⚠️ **这条定时器从来没有真正跑起来过**（2026-10 修）：

    到期判定把 tz-aware 的 `_now()` 绑给 naive 列 `quota_reset_at` 比较，
    asyncpg 每次都抛 `can't subtract offset-naive and offset-aware datetimes`，
    而外层 `except Exception: pass` 把它吃得干干净净 —— 没日志、没指标、没告警。
    表现是「跨月后没人配额自动恢复」，用尽 3001 的用户永久卡住，只能靠 admin
    手动 POST `reset-monthly` 补救。同一个坑 CP1.7.3 已经在**写入**路径上踩过一次
    （见 `next_reset_at_naive` 的说明），当年只修了写、没修比。

    两处一起改，否则「修好」比「坏掉」更糟：
      1. 比较用 naive 时间（让 tick 真能跑起来，见 `_reset_due_users`）；
      2. 重置 scope 到到期名单（让跑起来的 tick 不会变成计费洞）。
    只做 1 的话，任意匿名注册（`quota_reset_at` 建号时为 NULL）都会命中到期判定，
    下一轮就把全平台 `quota_used` 清零。
    """
    from stashbox.backend.common.database import AsyncSessionLocal

    if session_factory is None:
        session_factory = AsyncSessionLocal

    while True:
        try:
            async with session_factory() as session:
                n = await _reset_due_users(session)
            if n:
                log.info("quota_reset_loop_tick", reset_users=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 这条定时器唯一的失败模式就是「跨月没人配额自动恢复」。静默处理等于
            # 把它藏到下个季度有人抱怨「配额怎么不恢复」为止 —— 至少要留日志 + 打点。
            log.error("quota_reset_loop_tick_failed", error=str(exc), exc_info=True)
            try:
                quota_metrics.quota_reset_loop_error_total.inc()
            except Exception:
                pass
        await asyncio.sleep(interval_sec)


def next_reset_at() -> datetime:
    """给 API 展示用：下月 1 号 00:00（UTC）。"""
    return _next_month_start(_now()).replace(tzinfo=timezone.utc)
