"""月留存口径的单测（2026-10-25）。

## 为什么用**真 PG** 而不是 mock

这个函数的全部价值在**口径**：哪个月、哪些事件算活跃、分子分母怎么取。
mock 掉 session 就等于把要验的东西 mock 掉了。
⇒ 照抄 `tests/admin/test_export_csv.py` 的做法：幂等建表 + 真插数据 + 真查。

## 口径（与 common/retention.py 的模块说明一一对应）

- 活跃 = 自然月内有任意一条埋点事件，且 `user_id > 0`
- 月留存 = 上月活跃 ∩ 本月活跃 / 上月活跃
- 自然月按 UTC 切

## 为什么用**固定的历史月份**（2021-03 / 2021-04）

测试库 `stashbox_test` 是**共享**的：别的用例留下的埋点行也在里面。
如果用「当前月」当窗口，别人的数据会算进分母 ⇒ 断言随别的用例的残留而漂。

⇒ 固定用一个没人用的历史窗口，并在 fixture 里把该窗口**清干净**：
既不受别人影响，也不留自己的残渣。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import Feedback
from stashbox.backend.common.models.base import Base
from stashbox.backend.common.retention import monthly_retention
from stashbox.backend.common.timebase import db_now

#: 测试窗口：上月 = 2021-03，本月 = 2021-04（`now` 落在 2021-04-20）
PREV_YEAR, PREV_MONTH = 2021, 3
CUR_YEAR, CUR_MONTH = 2021, 4

#: 本文件专用的 user_id —— 用来隔离「往**当前月**插的行」，
#: 免得污染共享测试库里的其它用例（也免得自己下次的用例被污染）。
LIVE_TEST_USER_ID = 900001


@pytest.fixture(autouse=True)
async def _ensure_table():
    """幂等建 feedback 表（埋点表），并把测试窗口清干净。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Feedback.__table__])
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(Feedback).where(Feedback.created_at < datetime(2022, 1, 1)),
        )
        # 本文件往当前月插的行（走「当前月」那条路径）—— 只删自己的
        await s.execute(delete(Feedback).where(Feedback.user_id == LIVE_TEST_USER_ID))
        await s.commit()
    yield
    # 用完再清一次：别的用例的窗口是「当前月」，不会碰 2021 年的行，
    # 但留着会污染**下一次**跑本文件。
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(Feedback).where(Feedback.created_at < datetime(2022, 1, 1)),
        )
        await s.execute(delete(Feedback).where(Feedback.user_id == LIVE_TEST_USER_ID))
        await s.commit()


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    """naive UTC —— 埋点表的 created_at 列是 `TIMESTAMP`（不带时区），
    插进去的值必须同口径，否则 PG 拒收。"""
    return datetime(year, month, day, hour, 0, 0)


NOW = _at(CUR_YEAR, CUR_MONTH, 20)


async def _event(user_id: int, when: datetime, type_: str = "audio_play_start") -> None:
    async with AsyncSessionLocal() as s:
        s.add(
            Feedback(
                user_id=user_id,
                article_id=f"art_{uuid.uuid4().hex[:24]}",
                type=type_,
                metadata_={"src": "retention_test"},
                created_at=when,
            )
        )
        await s.commit()


async def test_上月活跃_本月仍活跃_算留存():
    """基本口径：上月 3 人活跃，其中 2 人本月还活跃 ⇒ 2/3。"""
    for uid in (1, 2, 3):
        await _event(uid, _at(PREV_YEAR, PREV_MONTH, 15))
    for uid in (1, 2):
        await _event(uid, _at(CUR_YEAR, CUR_MONTH, 2))

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=NOW)

    assert r["month"] == "2021-03"
    assert r["current_month"] == "2021-04"
    assert r["cohort_size"] == 3
    assert r["retained"] == 2
    assert r["rate"] == pytest.approx(2 / 3)


async def test_匿名事件不计入():
    """user_id=0 是匿名上报的约定（见 common/event_collect.py）⇒ 不算活跃。"""
    await _event(0, _at(PREV_YEAR, PREV_MONTH, 10))
    await _event(1, _at(PREV_YEAR, PREV_MONTH, 11))
    await _event(1, _at(CUR_YEAR, CUR_MONTH, 1))

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=NOW)

    assert r["cohort_size"] == 1  # 只有 user 1，匿名那条不算
    assert r["retained"] == 1


async def test_没有样本时_rate是None而不是0():
    """「0% 留存」与「没有样本」是两回事 ⇒ rate 必须能区分。"""
    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=NOW)

    assert r["cohort_size"] == 0
    assert r["retained"] == 0
    assert r["rate"] is None


async def test_月份边界归属正确():
    """上月**最后一天**算上月，本月**第一天**算本月。"""
    await _event(1, _at(PREV_YEAR, PREV_MONTH, 31, 23))
    await _event(1, _at(CUR_YEAR, CUR_MONTH, 1, 1))

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=NOW)

    assert r["cohort_size"] == 1
    assert r["retained"] == 1


async def test_跨年_上月是上一年十二月():
    """1 月算留存时，被考察的月份是**上一年**的 12 月。"""
    await _event(1, _at(2020, 12, 20))
    await _event(1, _at(2021, 1, 5))

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=_at(2021, 1, 20))

    assert r["month"] == "2020-12"
    assert r["current_month"] == "2021-01"
    assert r["cohort_size"] == 1
    assert r["retained"] == 1


async def test_本月新用户_不计入上月分母():
    """本月才第一次活跃的用户，不是「上月活跃用户」的留存对象。"""
    await _event(1, _at(PREV_YEAR, PREV_MONTH, 10))  # 上月活跃
    await _event(1, _at(CUR_YEAR, CUR_MONTH, 5))  # 上月那个又活跃了
    await _event(99, _at(CUR_YEAR, CUR_MONTH, 6))  # 本月新客

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=NOW)

    assert r["cohort_size"] == 1
    assert r["retained"] == 1


async def test_带时区的_now_不与naive列冲突():
    """**回归**：生产的 `now` 是 `datetime.now(timezone.utc)`（带时区）。

    埋点表的 `created_at` 是 naive 的 `TIMESTAMP` ⇒ 把带时区的值当查询上界
    传给 asyncpg，会报
    `can't subtract offset-naive and offset-aware datetimes`。

    实测踩过（2026-10-25）：整个 `/api/v1/admin/stats` 端点 500，
    `tests/admin/test_stats_enhanced.py` 的 8 条用例全红 —— 而本文件里
    其它用例都传的是 **naive** 的 `now`，所以一条都没红（**测试没覆盖生产路径**）。

    ⇒ 这条用例专门钉住「带时区的 now」这条路。
    """
    now_aware = datetime(2021, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    for uid in (1, 2):
        await _event(uid, _at(PREV_YEAR, PREV_MONTH, 15))
    await _event(1, _at(CUR_YEAR, CUR_MONTH, 2))

    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s, now=now_aware)

    assert r["month"] == "2021-03"
    assert r["current_month"] == "2021-04"
    assert r["cohort_size"] == 2
    assert r["retained"] == 1


async def test_不传_now_走生产默认路径也不炸():
    """不传 `now` = 生产的调用方式（`datetime.now(timezone.utc)`）。"""
    async with AsyncSessionLocal() as s:
        r = await monthly_retention(s)

    assert r["current_month"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert r["rate"] is None or 0.0 <= r["rate"] <= 1.0


async def test_不传now时_窗口上界用库里的钟而不是UTC():
    """**回归**（2026-10-25 生产实测）：窗口上界必须取**库里的钟**。

    背景：库里 `feedback.created_at` 存的是 PG session 时区（生产 Asia/Shanghai）
    的**墙上时间**。而 `datetime.now(timezone.utc)` 比它**慢 8 小时**
    ⇒ 用它当「本月至今」的上界 ⇒ **最近 8 小时的活跃记录被漏掉**，
    而且**不报任何错**（留存被系统性低估）。

    ⇒ 判据用**两个调用对比**（而不是绝对值）：同一个用户，
      一行在上月、一行在「库里的现在」；
      只有当上界取自**库里的钟**时，那一行才被算进「本月仍活跃」。

    ⚠️ 写成 `>=` 而不是 `==`：共享测试库里别的用例也会往当前月插行
    （`test_export_csv.py` 不给 created_at ⇒ PG 用 now() 填），
    它们的行可能正好落在这 8 小时里 ⇒ 差值可能是 1 也可能更大。
    """
    async with AsyncSessionLocal() as s:
        now_local = await db_now(s)
        # 上个月的 15 号（naive，库里的钟）
        # ⚠️ `replace(day=1)` **保留时分秒** ⇒ 只减 1 秒跨不到上个月
        #    （2026-10-01 23:18 - 1s 还是 10-01）⇒ 必须先把时分清零再减。
        #    （第一版就栽在这儿：行插到了**本月**，分母里根本没这个人，
        #     两个调用自然一样，断言看着像"实现没生效"。）
        first_of_this_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month = first_of_this_month - timedelta(seconds=1)
        last_month_day = prev_month.replace(day=15, hour=12, minute=0, second=0, microsecond=0)
        assert last_month_day < first_of_this_month  # 防止再算错月
        s.add(
            Feedback(
                user_id=LIVE_TEST_USER_ID,
                article_id=f"art_{uuid.uuid4().hex[:24]}",
                type="audio_play_start",
                metadata_={"src": "retention_clock_test"},
                created_at=last_month_day,
            )
        )
        # 本月的一行：created_at = 库里的现在（naive，库里的钟）
        s.add(
            Feedback(
                user_id=LIVE_TEST_USER_ID,
                article_id=f"art_{uuid.uuid4().hex[:24]}",
                type="audio_play_start",
                metadata_={"src": "retention_clock_test"},
                created_at=now_local,
            )
        )
        await s.commit()

    # ① 生产路径：不传 now ⇒ 上界取自库里的钟
    async with AsyncSessionLocal() as s:
        r_db_clock = await monthly_retention(s)
        # ② 旧写法：上界用 Python 的 UTC now（比库里的钟慢 8 小时）
        utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        r_utc_clock = await monthly_retention(s, now=utc_naive)

    # 我的用户上月活跃 ⇒ 在分母里；只有 ① 会把它算成「本月仍活跃」
    assert r_db_clock["retained"] >= r_utc_clock["retained"] + 1
