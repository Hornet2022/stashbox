"""时间口径：库里存的时间到底是**哪个钟**。

## 事实（2026-10-25 在生产实测出来的）

生产 PG 的 `TimeZone` 是 **Asia/Shanghai**：

    stashbox=# SHOW TimeZone;
     TimeZone
    ---------------
     Asia/Shanghai

⇒ `now()` 返回的是**本地墙上时间**，
而 `feedback.created_at` 这类 `TIMESTAMP`（**不带时区**）列存的就是它。

## 为什么这件事必须钉死（踩过的坑）

第一次实现客户端性能指标时，我按 `common/retention.py` 的注释理解成
「库里 created_at 是 UTC」，于是窗口上界用了 `datetime.now(timezone.utc)`。

**实测结果：生产明明有当天 21:19 的样本，查出来 `samples=0`。**

原因：UTC now 比库里的钟**慢 8 小时**（15:12 vs 23:12）
⇒ 窗口上界落在当天 15:12 ⇒ **最近 8 小时的样本全被漏掉**。
在数据量大时，这表现为「指标看起来永远是空的 / 偏旧」，而且**不报任何错**。

⇒ 教训：**别相信"这个时间字段是什么时区"的二手描述，去 `SHOW TimeZone` 看一眼。**

## 契约

- 查 `created_at` 这类列时，窗口边界必须与库里**同一个钟**：
  - 默认取库里的 `now()`（`SELECT now()`）—— 它的时区就是 `created_at` 的时区；
  - 传入 **naive** 的 `now` ⇒ 视为**已经是库里的钟**（测试里的惯例）；
  - 传入**带时区**的 `now` ⇒ 它是一个**绝对时刻**，
    交给数据库换算到它自己的钟（`AT TIME ZONE current_setting('TimeZone')`），
    而不是由 Python 猜一个时区。

- 返回值一律是 **naive** datetime（与 `created_at` 同口径，可直接比较）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 取「库里的现在」，并且是 **naive** 的墙上时间。
#:
#: ⚠️ 为什么不能直接写 `SELECT now()`：
#: `now()` 的返回类型是 **timestamptz**（带时区），asyncpg 会把它交给 Python 的
#: **aware** datetime ⇒ 拿去和 `created_at`（naive 列）比较时，asyncpg 直接报
#: `can't subtract offset-naive and offset-aware datetimes`（实测踩过）。
#:
#: `AT TIME ZONE current_setting('TimeZone')` 把它转成 **timestamp**
#: （不带时区、墙上时间）⇒ 既是 naive，又与 `created_at` 同钟。
_NOW_SQL = "SELECT now() AT TIME ZONE current_setting('TimeZone')"

#: 带时区输入的换算：绝对时刻 ⇒ 库里的钟。
_NOW_FROM_TS_SQL = "SELECT (:ts)::timestamptz AT TIME ZONE current_setting('TimeZone')"


async def db_now(db: AsyncSession, *, now: datetime | None = None) -> datetime:
    """「现在」，口径与库里 `created_at` 完全一致（naive，库 session 时区的墙上时间）。

    Args:
        db: 异步 session。
        now: 可选。`None` ⇒ 取库里的 `now()`；
             naive ⇒ 视为已在库里的钟，直接用；
             带时区 ⇒ 当作绝对时刻，交给数据库换算到它自己的钟。
    """
    if now is None:
        value = await db.scalar(text(_NOW_SQL))
        if value is None:  # pragma: no cover - PG 不会返回 NULL
            raise RuntimeError("SELECT now() 返回了 NULL")
        return value
    if now.tzinfo is None:
        return now
    # 带时区 = 绝对时刻 ⇒ 让数据库换算（Python 不知道库里的时区）
    value = await db.scalar(text(_NOW_FROM_TS_SQL), {"ts": now})
    if value is None:  # pragma: no cover
        raise RuntimeError("时区换算返回了 NULL")
    return value
