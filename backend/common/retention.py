"""月留存（CP6 埋点口径）。

## 为什么以前算不出来

仓里对留存的定义是 **≥ 50%**（`docs/技术方案_v1.md` §11.6 CP6 验收标准；
`docs/archive/cp10.9-product-audit_2026-09-20.md` §4「月留存 ≥ 50%」）。

但**没有「活跃」这个事实的来源**：

- `users` 表**没有** `last_active_at` 这一列 ——
  `content-service/admin_router.py:1789` 写得很清楚：
  「last_active_at 本仓库 users 表未建该列（v1 §4.2.1 未列），列位保留但恒为空」。
  ⇒ `cp10.9` 里那句「users.last_active_at 有字段但无定时任务计算留存」**是错的**：
  字段在 schema 里，但库里根本没建这一列。
- 没有 DAU 追踪表。

⇒ 唯一真实存在的数据源是**埋点表** `feedback`：`analytics.track()` 落在这里
（`id / user_id / article_id / type / metadata_ / created_at`），
服务端各个动作（剪藏、蒸馏、播放、评分…）都在打点。

## 口径（从仓内既有定义 import，不另造）

- **活跃** = 该自然月内有**任意一条**埋点事件，且 `user_id > 0`
  （`user_id = 0` 是匿名上报的约定，见 `common/event_collect.py`）。
- **月留存** = 上月活跃用户里，本月仍然活跃的比例。
  分母 = 上月活跃用户数；分子 = 上月活跃 ∩ 本月活跃。
- 自然月按 **UTC** 切，与库里 `created_at` 的时区口径一致。

## 为什么要单独成模块

口径一旦散在 SQL 里，就会出现「三个地方三个算法」的漂移。这里把它收成一个函数，
配套单测直接钉住边界（月初/月末、跨年、匿名事件不算、分母为 0）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import Feedback


def _to_naive_utc(moment: datetime) -> datetime:
    """统一成 **naive UTC**。

    为什么必须统一：埋点表的 `created_at` 列是 `TIMESTAMP`（**不带时区**，
    server_default 是 `func.now()`）。⇒ 比较时必须用同一种口径，
    否则 PG 直接报 `can't compare offset-naive and offset-aware datetimes`。
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _month_start(moment: datetime) -> datetime:
    """所在自然月的起点（UTC，闭区间起点）。"""
    return _to_naive_utc(moment).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def monthly_retention(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """算上个月的月留存。

    Returns:
        {
          "month": "2026-09",          # 被考察的月份（上月）
          "cohort_size": 12,            # 上月活跃用户数（分母）
          "retained": 7,                # 其中本月仍活跃的人数（分子）
          "rate": 0.5833,               # retained / cohort_size；分母为 0 时为 None
          "current_month": "2026-10",   # 现在的自然月
        }

    ⚠️ 分母为 0（没人活跃）时 `rate` 是 **None** 而不是 0 ——
    「0% 留存」和「没有样本」是两回事，混成 0 会让看板显示一个假的坏消息。
    """
    moment = now or datetime.now(timezone.utc)
    # ⚠️ 必须先统一成 naive UTC 再往下传。
    #
    # 埋点表的 `created_at` 是 `TIMESTAMP`（**不带时区**），而 `now` 的默认值
    # 是 `datetime.now(timezone.utc)`（**带时区**）。
    # ⇒ 直接把带时区的值当查询上界传给 asyncpg，会报
    #    "can't subtract offset-naive and offset-aware datetimes"
    # （实测：整个 admin stats 端点 500，8 条 stats 测试全红）。
    #
    # 注意 `_month_start()` 内部也做了同样的归一，但那只覆盖了**月份起点**；
    # 「本月至今」的上界用的还是原始 `moment` —— 这里必须自己先归一。
    naive_now = _to_naive_utc(moment)
    current_start = _month_start(naive_now)
    prev_start = _month_start(current_start - timedelta(seconds=1))

    async def _active_users(start: datetime, end: datetime) -> set[int]:
        """该时间窗内活跃过的用户集合。"""
        rows = await db.execute(
            select(Feedback.user_id)
            .where(
                Feedback.user_id > 0,
                Feedback.created_at >= start,
                Feedback.created_at < end,
            )
            .distinct()
        )
        return {int(uid) for uid in rows.scalars().all()}

    # ⚠️ 刻意用「两次取集合 + Python 求交」，而不是一条
    # `count(distinct) where user_id in (子查询)` 的 SQL。
    #
    # 那条 SQL 写起来短，但**同表子查询 + distinct** 的组合实测出过口径错
    # （分子把上月没在本月出现的人也算进去）。而这里要验的恰恰是口径 ——
    # 「上月活跃 ∩ 本月活跃」用集合交最直白，也不会被优化器改写。
    # 埋点表是运营量级（内测期每天几千行），两次 distinct 查询完全够用。
    prev_active = await _active_users(prev_start, current_start)
    cur_active = await _active_users(current_start, naive_now)

    cohort = len(prev_active)
    kept = len(prev_active & cur_active)
    return {
        "month": prev_start.strftime("%Y-%m"),
        "cohort_size": cohort,
        "retained": kept,
        "rate": (kept / cohort) if cohort else None,
        "current_month": current_start.strftime("%Y-%m"),
    }
