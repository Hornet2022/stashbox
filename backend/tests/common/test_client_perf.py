"""客户端性能口径（首屏 / 音频启动）的单测（2026-10-25）。

## 为什么用**真 PG** 而不是 mock

和 `tests/common/test_retention.py` 同一个理由：这个函数的全部价值在**口径** ——
哪些事件算、窗口怎么切、无效的 duration_ms 怎么算。
mock 掉 session 就等于把要验的东西 mock 掉了。

## 为什么用**固定的历史窗口**（2021-05）

测试库 `stashbox_test` 是**共享**的：别的用例留下的埋点行也在里面。
用「当前时间」当窗口 ⇒ 别人的数据会算进来，断言随别的用例的残留而漂。

⇒ 固定用一个没人用的历史窗口（2021-05），并在 fixture 里把该窗口**清干净**。

⚠️ 清的**范围要窄**（只清 2021-05 那个月）：
`test_retention.py` 的 fixture 清的是「2022 年之前的所有行」——
如果这边也那么清，会把它的窗口一起抹掉（两个文件都跑时互相干扰）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from stashbox.backend.common.client_perf import (
    TARGET_AUDIO_START_MS,
    TARGET_FIRST_FRAME_MS,
    client_perf_stats,
    quantile,
)
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.events import EventName
from stashbox.backend.common.models import Feedback
from stashbox.backend.common.models.base import Base

#: 测试窗口：now = 2021-05-20，7 天窗口 ⇒ 2021-05-13 ~ 2021-05-20
NOW = datetime(2021, 5, 20, 12, 0, 0)
WINDOW_LO = datetime(2021, 5, 1)
WINDOW_HI = datetime(2021, 6, 1)


@pytest.fixture(autouse=True)
async def _ensure_table():
    """幂等建 feedback 表，并把**我这个窗口**清干净（不碰别的文件的窗口）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Feedback.__table__])
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Feedback).where(Feedback.created_at >= WINDOW_LO))
        await s.execute(delete(Feedback).where(Feedback.created_at < WINDOW_HI))
        await s.commit()
    yield
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Feedback).where(Feedback.created_at >= WINDOW_LO))
        await s.execute(delete(Feedback).where(Feedback.created_at < WINDOW_HI))
        await s.commit()


async def _event(
    when: datetime,
    type_: str,
    metadata: dict | None = None,
) -> None:
    """插一条埋点。metadata 传 None = 客户端没带任何属性。"""
    async with AsyncSessionLocal() as s:
        s.add(
            Feedback(
                user_id=0,
                article_id=f"art_{uuid.uuid4().hex[:24]}",
                type=type_,
                metadata_=metadata,
                created_at=when,
            )
        )
        await s.commit()


def _at(day: int, hour: int = 12) -> datetime:
    """naive UTC —— 埋点表的 created_at 是 TIMESTAMP（不带时区）。"""
    return datetime(2021, 5, day, hour, 0, 0)


# ── 口径 ────────────────────────────────────────────────────────────────


async def test_基本口径_p50_p95_样本量():
    """4 个样本 [100, 200, 300, 400] ⇒ P50 = 250、P95 = 385、samples = 4。

    P50 用线性插值（与 PG 的 percentile_cont 同一套算法）：
    pos = (4-1) × 0.5 = 1.5 ⇒ 200 + (300-200) × 0.5 = 250。
    """
    for i, ms in enumerate([100, 200, 300, 400]):
        await _event(
            _at(14 + i // 4),
            EventName.APP_FIRST_FRAME.value,
            {"duration_ms": ms, "device_id": f"d{i}"},
        )

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    first = r["first_frame"]
    assert first["samples"] == 4
    assert first["p50_ms"] == pytest.approx(250.0)
    assert first["p95_ms"] == pytest.approx(385.0)
    assert first["event"] == "app_first_frame"


async def test_没有样本时_p50是None而不是0():
    """「没有样本」与「0ms」是两回事 ⇒ 分位数必须是 None。

    渲染成 0ms 会显示一个假的「启动很快」—— 这是比缺字段更坏的错误。
    """
    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 0
    assert r["first_frame"]["p50_ms"] is None
    assert r["first_frame"]["p95_ms"] is None
    assert r["audio_start"]["samples"] == 0
    assert r["audio_start"]["p50_ms"] is None


async def test_无效的duration_ms不算样本():
    """缺字段 / 不是数字 / ≤ 0 / 布尔值，一律**不算样本**。

    为什么：如果把它们当 0 算进去，P50 会被拉成「启动 0ms」——
    一个**假的乐观结论**，比没有数据更危险。
    """
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, None)
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": "380"})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": 0})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": -5})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": True})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 0
    assert r["first_frame"]["p50_ms"] is None


async def test_有效样本和无效样本混在一起时只算有效的():
    """一半有效一半无效 ⇒ 只按有效的算，样本量也要如实反映。"""
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": 500})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": 700})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": "oops"})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 2
    assert r["first_frame"]["p50_ms"] == pytest.approx(600.0)


async def test_窗口外的事件不算():
    """7 天窗口外的事件不参与（8 天前那一条要被排除）。"""
    await _event(_at(19), EventName.APP_FIRST_FRAME.value, {"duration_ms": 100})
    # 窗口起点是 NOW - 7 天 = 2021-05-13 ⇒ 12 号在窗口外
    await _event(_at(12), EventName.APP_FIRST_FRAME.value, {"duration_ms": 9999})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 1
    assert r["first_frame"]["p50_ms"] == pytest.approx(100.0)


async def test_窗口起点当天算窗口内():
    """边界：窗口是**左闭右开**的 `[NOW-7天, NOW)`。

    ⚠️ 窗口起点是**时间戳**（NOW - 7 天 = 05-13 12:00），不是日期边界 ——
    05-13 00:00 那一条在窗口**外**。这条用例专门钉住这个容易想当然的地方。
    """
    # 正好在起点上 ⇒ 算（左闭）
    await _event(
        datetime(2021, 5, 13, 12, 0, 0), EventName.APP_FIRST_FRAME.value, {"duration_ms": 100}
    )
    # 起点前一秒 ⇒ 不算
    await _event(
        datetime(2021, 5, 13, 11, 59, 59),
        EventName.APP_FIRST_FRAME.value,
        {"duration_ms": 9999},
    )
    # 正好在 NOW 上 ⇒ 不算（右开）
    await _event(NOW, EventName.APP_FIRST_FRAME.value, {"duration_ms": 8888})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 1
    assert r["first_frame"]["p50_ms"] == pytest.approx(100.0)


async def test_音频启动只认客户端那个事件名():
    """服务端打的 `audio_play_start` **不能**算进音频启动指标。

    ⚠️ 这条是口径里最容易搞错的一处：
    `audio_play_start` = 「用户点了播放 → 前端来取音频直链」（服务端）；
    `audio_play_start_client` = 「播放器真的出声了」（客户端，带 duration_ms）。
    两者同名会让 A/B 报表的播放次数翻倍（见 common/events.py 的说明）。
    ⇒ 音频启动指标只认 `_client` 那条。
    """
    await _event(_at(15), EventName.AUDIO_PLAY_START.value, {"duration_ms": 111})
    await _event(_at(15), EventName.AUDIO_PLAY_START_CLIENT.value, {"duration_ms": 380})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["audio_start"]["samples"] == 1
    assert r["audio_start"]["p50_ms"] == pytest.approx(380.0)
    assert r["audio_start"]["event"] == "audio_play_start_client"
    # 服务端那条不该污染首屏指标
    assert r["first_frame"]["samples"] == 0


async def test_两个指标互不串味():
    """首屏事件不该跑进音频启动指标，反之亦然。"""
    await _event(_at(15), EventName.APP_FIRST_FRAME.value, {"duration_ms": 1234})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["samples"] == 1
    assert r["audio_start"]["samples"] == 0


async def test_目标线来自仓内既有定义():
    """目标线不能拍脑袋：首屏 1500ms、音频启动 500ms（cp10.9 §4 的「7 大指标」）。"""
    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["first_frame"]["target_ms"] == TARGET_FIRST_FRAME_MS == 1500
    assert r["audio_start"]["target_ms"] == TARGET_AUDIO_START_MS == 500


async def test_窗口元信息():
    """看板要能说清「这是哪 7 天」⇒ 窗口起止要一起给。"""
    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW)

    assert r["window_days"] == 7
    assert r["window_start"] == "2021-05-13"
    assert r["window_end"] == "2021-05-20"


async def test_带时区的now_不与naive列冲突():
    """**回归**：生产的 `now` 是 `datetime.now(timezone.utc)`（带时区）。

    埋点表的 `created_at` 是 naive 的 `TIMESTAMP` ⇒ 把带时区的值当查询边界
    传给 asyncpg 会报 "can't subtract offset-naive and offset-aware datetimes"。
    """
    await _event(_at(15), EventName.APP_FIRST_FRAME.value, {"duration_ms": 1234})

    aware_now = datetime(2021, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=aware_now)

    assert r["first_frame"]["samples"] == 1
    assert r["first_frame"]["p50_ms"] == pytest.approx(1234.0)


async def test_自定义窗口天数():
    """窗口天数可调（看板写死 7 天，但口径本身不该写死）。"""
    await _event(_at(19), EventName.APP_FIRST_FRAME.value, {"duration_ms": 100})
    await _event(_at(14), EventName.APP_FIRST_FRAME.value, {"duration_ms": 200})

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s, now=NOW, window_days=3)

    assert r["window_days"] == 3
    assert r["window_start"] == "2021-05-17"
    assert r["first_frame"]["samples"] == 1  # 只有 19 号那条


async def test_用库里的时钟_当天样本不会因为差8小时而漏掉():
    """**回归**（2026-10-25 生产实测）：窗口上界必须取**库里的钟**。

    库里 `feedback.created_at` 存的是 PG session 时区（生产 Asia/Shanghai）的
    **墙上时间**；而 `datetime.now(timezone.utc)` 比它**慢 8 小时**
    ⇒ 用 UTC now 当上界 ⇒ **最近 8 小时的样本全被漏掉**，
    而且**不报任何错**（生产实测：库里明明有当天 21:19 的样本，
    接口返回 `samples=0`）。

    ⇒ 这条不传 `now`（走生产路径），插一行**不指定 created_at**
    （让 PG 用自己的 now() 填）⇒ 它必须被算进样本。
    """
    async with AsyncSessionLocal() as s:
        s.add(
            Feedback(
                user_id=0,
                article_id=f"art_{uuid.uuid4().hex[:24]}",
                type=EventName.APP_FIRST_FRAME.value,
                metadata_={"duration_ms": 1234, "device_id": "live-row"},
                # ⚠️ **不写 created_at** ⇒ 走 server_default = now()（库的钟）
            )
        )
        await s.commit()

    async with AsyncSessionLocal() as s:
        r = await client_perf_stats(s)

    assert r["first_frame"]["samples"] == 1
    assert r["first_frame"]["p50_ms"] == pytest.approx(1234.0)


# ── quantile 纯函数（脱离 DB 单测）──────────────────────────────────────


def test_quantile_空列表返回None():
    """空 ⇒ None（**不是 0**）。"""
    assert quantile([], 0.5) is None


def test_quantile_单元素():
    assert quantile([123.0], 0.5) == 123.0
    assert quantile([123.0], 0.95) == 123.0


def test_quantile_首尾():
    """q=0 取最小、q=1 取最大。"""
    assert quantile([10, 20, 30], 0.0) == 10.0
    assert quantile([10, 20, 30], 1.0) == 30.0


def test_quantile_奇数个样本_取正中那个():
    """n=3 时 P50 就是中间那个（不插值）。"""
    assert quantile([10, 20, 30], 0.5) == 20.0


def test_quantile_偶数个样本_线性插值():
    """n=2 时 P50 = 中点（这正是与 percentile_cont 对齐的地方）。"""
    assert quantile([10, 30], 0.5) == 20.0
    assert quantile([0, 100], 0.95) == pytest.approx(95.0)


def test_quantile_不依赖输入顺序():
    """传进来乱序也要出同样的结果（排序在函数内部做）。"""
    assert quantile([30, 10, 20], 0.5) == quantile([10, 20, 30], 0.5)


def test_quantile_越界的q_报错():
    """q 不在 [0,1] ⇒ 明确报错，而不是静默算一个数。"""
    with pytest.raises(ValueError):
        quantile([1, 2], 1.5)
