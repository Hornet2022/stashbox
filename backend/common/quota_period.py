"""配额周期（自然月）的唯一时点定义。

## 为什么单独一个模块

「配额什么时候重置」这件事，仓里散着三份实现：

- `common/quota_service.py::_next_month_start` / `next_reset_at_naive`
  —— 运行时真正在用的那份（写 `users.quota_reset_at`、比较是否到期）；
- `alembic/versions/0034_backfill_quota_reset_at.py::_next_month_start_naive`
  —— 迁移里的冻结副本（迁移不该依赖会变的应用代码，这是对的）；
- 以及各服务自己按「一个月」推算的地方。

第三类才是问题：**「一个月」被当成了一个魔数**。`common/cache_service.py` 的
「文章已扣配额」标记原来写死 `ex=86400`（24 小时），而配额其实是**自然月**
重置的 —— 两者根本不是同一个时间尺度，于是：

    剪藏（已扣费）→ 蒸馏没派出去 → 次日手动点「开始蒸馏」
    → 标记已过期 → 又扣一次 → 同一个配额周期内为同一篇付两次钱。

现在这个模块是**唯一的运行时定义**：`quota_service` 与 `cache_service`
都从这里取配额周期的边界，谁也不再自己推算「下个月 1 号」。

## 为什么用自然月而不是「30 天」

配额是**自然月**重置的（`users.quota_reset_at` = 下月 1 号 00:00 UTC）。
所以「当前配额周期」的长度在 28~31 天之间浮动，写死 30 天会在某些月份
差一两天。统一从「下月 1 号」推，边界就只有一个真相。
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """当前 UTC 时间（tz-aware）。

    单独抽出来是为了让测试能替换"现在"，
    而不必去 monkeypatch 各个调用方各自的 `_now()`。
    """
    return datetime.now(timezone.utc)


def next_month_start(now: datetime) -> datetime:
    """`now` 所在月份的下个月 1 号 00:00（**tz-aware**，保持 now 的 tzinfo）。

    `now` 应当是 tz-aware 的 UTC 时间（用 `now_utc()` 取）。
    """
    if now.month == 12:
        return now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return now.replace(
        month=now.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def next_reset_at_naive(now: datetime | None = None) -> datetime:
    """下月 1 号 00:00（UTC，**naive**）—— 碰 `users.quota_reset_at` 唯一正确的值。

    该列是 `TIMESTAMP WITHOUT TIME ZONE`（alembic 0002），而 datetime.now(utc)
    是 tz-aware。asyncpg 拿到 tz-aware datetime 绑到 naive 列会直接抛：
    DataError: can't subtract offset-naive and offset-aware datetimes

    所以凡是**写或比**这一列都必须用它，不要各处自己 `.replace(tzinfo=None)`。
    分散写正是它出事的根因：CP1.7.3 修了写入路径（`reset_monthly`），而
    `quota_reset_loop` 的比较路径漏了，于是整条跨月重置从来没跑起来过 ——
    而那正是这条路径存在的唯一理由。

    （这段警示原本写在 `quota_service.next_reset_at_naive` 上。定义搬到本模块后，
    引用点仍可照旧用 `quota_service.next_reset_at_naive`，两者是同一个函数。）
    """
    base = now or now_utc()
    return next_month_start(base).replace(tzinfo=None)


def seconds_until_next_reset(now: datetime | None = None) -> int:
    """距离下一次配额重置还有多少秒（至少 60 秒）。

    这是「文章已扣配额」标记该活多久的判据：

    - 标记记的是**事实** —— 「这篇文章在当前配额周期内已经扣过费了」；
    - 配额周期到「下月 1 号」结束，所以标记活到重置点就够：
      周期内不会重复扣；周期结束后配额也重置了，再扣是新周期的正常计费；
    - 用 24 小时（原来的值）就短了一整个配额周期 → 同周期内重复扣费。
    """
    base = now or now_utc()
    delta = (next_month_start(base) - base).total_seconds()
    return max(60, int(delta))
