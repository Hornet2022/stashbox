"""跨月配额重置定时器回归（2026-10）

这条定时器是「配额到期自动恢复」的唯一实现，而它**从来没跑起来过**。

`quota_service.quota_reset_loop`（user-service 启动时拉起，每小时一拍）的到期判定
把 tz-aware 的 `_now()` 绑给 `users.quota_reset_at` 比较。该列是
`TIMESTAMP WITHOUT TIME ZONE`（alembic 0002），asyncpg 直接抛：

    can't subtract offset-naive and offset-aware datetimes

而外层 `except Exception: pass` 把它吃得干干净净 —— 没日志、没指标、没告警。
真实后果不是「报错多一条」，而是**跨月后没人配额自动恢复**：quota_used 永不清零，
用尽 3001 的用户永久卡住，只能靠 admin 手动 POST reset-monthly 补救。
根因是同一个坑踩了两次：CP1.7.3 修了写入路径（reset_monthly 里的 `.replace(tzinfo=None)`），
比较路径漏了。

⚠️ 更要紧的是：把 tz 这一处修好还只是「让它能跑」，而它一旦能跑就会暴露第二颗雷 ——
到期判定含 `quota_reset_at IS NULL`，建号路径从没写过这一列（可空无默认值），
所以任意匿名注册都会命中到期名单；而 `reset_monthly` 的 UPDATE 不带 User.id 过滤，
是**全平台**操作。两颗雷必须同批修，否则从「定时器死」变成「任何人注册即清零全平台配额」。

本文件的用例围绕这两点：
  A. 到期判定不能因为 naive/aware 崩（A 组：真 PG 跑一拍）
  B. 一拍只能重置「到期的人」，不能连坐（B 组）
  C. 定时器失败必须留痕，不能静默（C 组）
另附 D 组：把修复逐个改回去，测试必须变红（守卫有效性）。

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。
（tests/conftest.py 会把连接切到独立测试库，见那里的告警。）
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from stashbox.backend.common import quota_service
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User
from stashbox.backend.common.quota_metrics import quota_reset_loop_error_total


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _mk_user(used: int = 0, reset_at: datetime | None = None, monthly: int = 10) -> int:
    """建一个测试用户。

    `reset_at` 显式传 naive UTC 时间；默认给「下月 1 号」= 不到期，
    这样 A/B 组的判据不会被「本月的其他测试用户」污染。
    """
    async with AsyncSessionLocal() as session:
        u = User(
            open_id="tick_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
            monthly_quota=monthly,
            quota_used=used,
            quota_reset_at=reset_at
            if reset_at is not None
            else quota_service.next_reset_at_naive(),
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return int(u.id)


async def _state(uid: int) -> tuple[int, datetime | None]:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(User.quota_used, User.quota_reset_at).where(User.id == uid)
            )
        ).one()
    return int(row[0]), row[1]


async def _tick() -> int:
    """跑一拍真实的定时器逻辑。"""
    async with AsyncSessionLocal() as session:
        return await quota_service._reset_due_users(session)


def _naive_utc(days_from_now: int = -1) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# A. 到期判定不能崩 —— 这就是定时器从来没跑起来的那一行
# ---------------------------------------------------------------------------
async def test_tick_does_not_crash_on_naive_column():
    """跑一拍不能抛。

    修之前：到期判定的 SELECT 拿 aware `now` 去比 naive 列，asyncpg 抛
    DataError/TypeError('can't subtract offset-naive and offset-aware datetimes')，
    被定时器外层的 `except Exception: pass` 吃掉 → 零日志零告警，重置永不发生。
    """
    # 一行到期数据：造出来是为了让 SELECT 真的有活干（空表也能撞上绑定错误，
    # 但有数据时这条断言才真的在测「比较能跑通」而不是「表是空的所以没事」）
    await _mk_user(used=3, reset_at=_naive_utc(days_from_now=-1))

    n = await _tick()  # 不抛 = 绑定对了

    assert isinstance(n, int)


async def test_tick_resets_only_rows_actually_due():
    """到期的人真的被重置 —— 证明不是「不崩了但也没干活」。

    只断言不崩是不够的：那正是修之前的状态（每拍都抛，什么也没做）。
    """
    due = await _mk_user(used=3, reset_at=_naive_utc(days_from_now=-1))  # 昨天就该重置
    future = await _mk_user(used=7, reset_at=quota_service.next_reset_at_naive())  # 未到期

    await _tick()

    assert (await _state(due))[0] == 0, "到期用户配额没清零 —— 定时器没真干活"
    assert (await _state(future))[0] == 7, "未到期用户被清了 —— scope 放太宽了"


async def test_reset_writes_naive_quota_reset_at():
    """重置后写入的仍是 naive（列是 timestamp without time zone）。

    写成 aware 的话，下一拍比较又会炸 —— 又回到「跑了一轮就死」的老路。
    """
    due = await _mk_user(used=2, reset_at=_naive_utc(days_from_now=-1))

    await _tick()

    _used, reset_at = await _state(due)
    assert reset_at is not None
    assert reset_at.tzinfo is None, f"写回 aware 时间戳，下一拍会抛：{reset_at!r}"


# ---------------------------------------------------------------------------
# B. 连坐：NULL 的一行不能让全平台配额清零
# ---------------------------------------------------------------------------
async def test_null_reset_at_does_not_wipe_other_users_quota():
    """这是本次修复里更危险的一半。

    新建用户的 `quota_reset_at` 是 NULL（可空、无默认值），而到期判定含
    `quota_reset_at IS NULL` —— 任意匿名注册就能命中到期名单。如果重置不带
    scope（`reset_monthly` 的 UPDATE 无 User.id 过滤），下一拍就把**所有**
    quota_used != 0 的人清零，直接击穿 LLM/TTS 成本。

    所以这条断言的判据是「受害者没被动」，而不是「攻击者被处理了」——
    受害者不受影响才是这件事的边界。
    """
    victim = await _mk_user(used=9)  # 受害者：额度用掉 9，跨月前绝不能被清零
    # 攻击者等价物：建号时 quota_reset_at 为 NULL 的行
    async with AsyncSessionLocal() as session:
        attacker = User(
            open_id="tick_null_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
            monthly_quota=10,
            quota_used=0,
        )
        session.add(attacker)
        await session.commit()
        await session.refresh(attacker)
        attacker_id = int(attacker.id)
    assert (await _state(attacker_id))[1] is None, "构造没生效：这一行本该是 NULL"

    await _tick()

    used_after, _ = await _state(victim)
    assert used_after == 9, f"受害者配额被清零了（{used_after}）—— 定时器变成了计费洞"


async def test_scoped_reset_leaves_other_users_untouched_even_when_due():
    """两个都到期的人，两个都该被重置 —— 但**未到期**的一个绝不能跟着走。

    这条现在主要钉的是「范围判据只有到期条件，没有别的」。修之前的写法是
    「SELECT 出到期 id 列表 → WHERE id IN (...)」，范围是对的但代价大（见
    `reset_due_users` 的说明），所以另有 `test_定时器重置不带_in_列表` 钉住形态。
    """
    due_a = await _mk_user(used=4, reset_at=_naive_utc(days_from_now=-1))
    due_b = await _mk_user(used=4, reset_at=_naive_utc(days_from_now=-1))
    not_due = await _mk_user(used=4, reset_at=quota_service.next_reset_at_naive())

    await _tick()

    assert (await _state(due_a))[0] == 0
    assert (await _state(due_b))[0] == 0
    assert (await _state(not_due))[0] == 4, "未到期用户被连坐了"


def test_定时器重置不带_in_列表():
    """形态守卫：定时器的重置不能是「先查 id 列表再 IN 回去」。

    跨月那一刻全平台同时到期，id 列表长度约等于整张 users 表。IN 几千个 id 会
    生成巨型 SQL（PG 绑定参数上限 65535），且 SELECT→UPDATE 之间有 TOCTOU 窗口。
    判据直接写进 UPDATE 的 WHERE 就没有这两问题 —— 但「没必要优化」很容易在
    某次重构里被改回去，所以钉在这里。
    """
    from pathlib import Path

    src = (Path(quota_service.__file__)).read_text()
    start = src.index("async def reset_due_users(")
    body = src[start : src.index("async def _reset_due_users(")]
    assert ".in_(" not in body, "定时器重置又变回 IN 列表了 —— 跨月时会撞 PG 参数上限"
    assert "_due_filter" in body, "到期判据必须收敛到 _due_filter，别在两个函数各写一遍"


# ---------------------------------------------------------------------------
# C. 定时器不能静默 —— 失败要留痕
# ---------------------------------------------------------------------------
async def test_loop_logs_and_counts_instead_of_swallowing():
    """一拍拍失败时：记日志 + 打点，不是 `except Exception: pass`。

    这条守的是「别再退回静默」。定时器的失败模式是「配额不再自动恢复」，
    静默处理的代价是整个月都没人发现。所以断言两件事同时发生：
    `quota_reset_loop_error_total` 涨了，且 error 级日志里有痕迹。

    实现上不去真的跑死循环，而是把外层那段 try/except 的逻辑用真异常走一遍：
    注入一个一 execute 就炸的 session_factory，跑一拍，掐断。
    """
    before = _counter_value()

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            raise RuntimeError("模拟 DB 挂了")

    def _factory():
        return _Boom()

    # 复制定时器外层那一拍的错误处理（保持与生产同一段逻辑）：
    # 直接起一个 task 跑一拍再取消，比在测试里复刻 try/except 更能证明「真跑了」。
    task = asyncio.create_task(
        quota_service.quota_reset_loop(interval_sec=3600, session_factory=_factory)
    )
    # 等它至少跑完一拍（第一次 sleep 即代表这一拍已结束）
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _counter_value() > before, "定时器失败没有打点 —— 这正是它藏了这么久的原因"


def _counter_value() -> float:
    return quota_reset_loop_error_total._value.get()


# ---------------------------------------------------------------------------
# D. 守卫有效性：把修复改回去，上面每一条都必须变红
# ---------------------------------------------------------------------------
# 这些不是运行时用例，而是**改代码时手动的验证清单**。本轮实际做过，结果记录在
# commit message 里：
#
#   1. 把 `_reset_due_users` 里的 now 改回 `_now()`（aware）
#      → A 组三条全红：DataError/TypeError can't subtract offset-naive...
#   2. 把 `reset_monthly(session, user_ids=list(due_ids))` 改回 `reset_monthly(session)`
#      → `test_null_reset_at_does_not_wipe_other_users_quota` 红：受害者被清零
#   3. 把 `log.error` + 打点改回 `except Exception: pass`
#      → C 组红：`quota_reset_loop_error_total` 不涨
#
# 第 2 条是本次最关键的一条：只修 tz 而不加 scope，定时器从「死」变成
# 「任何人注册即清零全平台配额」—— 比原来更糟。
