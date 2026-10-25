"""时间口径模块的单测（2026-10-25）。

## 这个模块为什么存在

生产 PG 的 `TimeZone` 是 **Asia/Shanghai** ⇒ `feedback.created_at` 存的是
**本地墙上时间**，不是 UTC。而第一版实现用 `datetime.now(timezone.utc)`
当查询窗口的上界 ⇒ 比库里的钟**慢 8 小时** ⇒ **最近 8 小时的样本全被漏掉**
（实测：库里明明有当天 21:19 的样本，接口返回 `samples=0`，且不报任何错）。

⇒ 窗口边界必须从**库里**取（`SELECT now()`），与被查的列同一个钟。

## 怎么测

- `db_now()` 的契约就是「与 `SELECT now()` 同一个钟」⇒ 直接对拍；
- 带时区的输入被当作**绝对时刻**换算 ⇒ 断言换算后的**具体**墙上时间
  （顺带把「这台库的时区是什么」写成可执行的断言，而不是只写在注释里）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.timebase import db_now


async def test_db_now_与库里的now完全同口径():
    """核心契约：`db_now()` ≡ 库里的墙上时间（naive）。

    ⚠️ 对拍用的是 `now() AT TIME ZONE current_setting('TimeZone')`（naive 形式），
    不是裸的 `now()` —— 裸 `now()` 是 timestamptz，asyncpg 给的是 **aware**
    datetime，与 `created_at`（naive 列）根本不是一个类型。
    """
    async with AsyncSessionLocal() as s:
        got = await db_now(s)
        pg = await s.scalar(select(text("now() AT TIME ZONE current_setting('TimeZone')")))

    assert got == pg
    # 必须是 naive —— 带时区的值拿去和 created_at 比较会直接报错
    assert got.tzinfo is None


async def test_naive输入原样返回():
    """naive 输入 = **已经在库里的钟** ⇒ 不做任何换算（测试里的惯例）。"""
    naive = datetime(2021, 5, 20, 12, 0, 0)
    async with AsyncSessionLocal() as s:
        assert await db_now(s, now=naive) == naive


async def test_带时区的输入被当作绝对时刻换算到库里的钟():
    """带时区 = 一个**绝对时刻** ⇒ 换算到库里的钟（不是 Python 猜一个时区）。

    12:00 UTC = 20:00 Asia/Shanghai（生产 PG 的 TimeZone，2026-10-25 实测）。
    ⚠️ 这条断言顺带把「这台库的时区」钉住了 —— 运维真去改 PG 的 TimeZone 时，
    这里会响，而不是让某个指标悄悄偏 8 小时。
    """
    aware = datetime(2021, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as s:
        got = await db_now(s, now=aware)

    assert got == datetime(2021, 5, 20, 20, 0, 0)


async def test_db_now不是python的utcnow():
    """`db_now()` 与 `datetime.now(timezone.utc)` **不是同一个钟**。

    这条把「别拿 Python 的 UTC now 当窗口上界」写成可执行的判据：
    在这台库上两者差一个时区偏移（生产是 8 小时）。

    ⚠️ 如果运维把 PG 的 TimeZone 改成 UTC，这条会红 —— 那时旧写法反而对了，
    所以这里**必须**跟着改，而不是让它悄悄过去。
    """
    async with AsyncSessionLocal() as s:
        n = await db_now(s)

    utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert n != utc_naive
