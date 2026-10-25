"""客户端性能指标（首屏 / 音频启动）的口径。

## 为什么要有这个模块

数据**早就在库里** —— 客户端上报的 `duration_ms` 落在 `feedback.metadata`
（2026-10-25 实测：生产库里 `app_first_frame` 与 `audio_play_start_client`
两行，metadata 里分别有 `duration_ms: 1234` 与 `duration_ms: 380`）。

但 admin 侧**没有聚合端点**，看板也没有卡 ⇒ 内测用户反馈"启动慢"时，
我们只能去查 `feedback` 表手写 SQL，看不到分位数、也没法按窗口对比。

⇒ 这里把口径收成一处（与 `common/retention.py` 平行：月留存是同一个道理，
"当前版本/当前状态"这类事实必须只有一个算法），配套单测直接钉住边界。

## 口径（从仓内既有定义 import，不另造常量）

| 项 | 取值 | 出处 |
|---|---|---|
| 首屏事件 | `app_first_frame` | `common/events.py` 的 `EventName.APP_FIRST_FRAME` |
| 音频启动事件 | `audio_play_start_client` | `common/events.py` 的 `EventName.AUDIO_PLAY_START_CLIENT` |
| 首屏目标线 | **1500 ms** | `cp10.9-product-audit` §4「7 大指标」的「首屏加载 < 1.5s」；`events.py` 的注释同源 |
| 音频启动目标线 | **500 ms** | 同上，「音频启动 < 500ms」 |
| 统计量 | **P50 + P95** | 仓里既有的时长指标都用这个口径（admin 的「蒸馏耗时」也是 P50 + P95） |
| 窗口 | 最近 **7 天**（UTC） | 与看板「近 7 天趋势」同一尺度 |

⚠️ **音频启动为什么是 `audio_play_start_client` 而不是 `audio_play_start`**：
服务端那条是「用户点了播放 → 前端来取音频直链」，客户端那条才是「真的出声了」。
两者同名会导致一次播放两条，把 A/B 报表的完听率/复听率算错（见 `events.py` 的说明）。

## 为什么用分位数而不用均值

启动耗时的分布**很宽**：冷启动 / 热启动、弱网 / 强网，差一个数量级。
均值会被长尾带跑（实测过一次教训：宽分布下中位数都在乱跳，要用分位数 + 独立指标交叉验证）。
⇒ 给 **P50（典型用户）** 和 **P95（最慢的 5%）**，两个一起看才说得清。

## 为什么必须带样本量

分位数的可信度锚点就是样本量 —— 「1 个样本的 P95」和「200 个样本的 P95」
长得一模一样，但完全不是一回事（仓里已有的规矩，见 admin「蒸馏耗时」的样本数列）。
⇒ `samples` 与分位数一起返回，看板侧必须一起显示。

## 「没有样本」不是「0ms」

`duration_ms` 缺失 / 不是数字 / ≤ 0 的行**不算样本**（见 `_extract_duration_ms`）。
没有样本时 `p50_ms` / `p95_ms` 是 **None**，不是 0 ——
渲染成 0ms 会显示一个假的「启动很快」。

看板侧还有**第三种**状态：「后端压根没这个字段」（还没部署 / 部署顺序反了），
那和「有字段但没样本」必须分开说 —— 见 admin-web 的 `ClientPerfCard`。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.events import EventName
from stashbox.backend.common.models import Feedback
from stashbox.backend.common.timebase import db_now

#: 统计窗口（天）。7 天 = 与看板「近 7 天趋势」同尺度；
#: 内测期样本少，24h 窗口经常一条样本都没有。
DEFAULT_WINDOW_DAYS = 7

#: 首屏加载的目标线（毫秒）。出处见模块说明表。
TARGET_FIRST_FRAME_MS = 1500

#: 音频启动（真的出声）的目标线（毫秒）。出处见模块说明表。
TARGET_AUDIO_START_MS = 500


def quantile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数（与 PG 的 `percentile_cont` 同一套算法）。

    为什么不用 PG 的 `percentile_cont`：
    ① 纯函数可以**脱离数据库**单测（边界值、空列表、偶数个样本的中点）；
    ② 不依赖特定数据库方言（测试用真 PG，但口径本身不绑死 SQL）。

    算法：`pos = (n - 1) * q`，取 `floor`/`ceil` 两个邻居线性插值。
    n=1 时直接返回那个值；空列表返回 None（**不是 0**）。
    """
    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q 必须在 [0, 1]，收到 {q}")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[int(pos)]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _extract_duration_ms(metadata: Any) -> float | None:
    """从一条埋点的 metadata 里取出 `duration_ms`。

    什么算有效样本：
    - 必须是 JSON **数字**（客户端上报的是整数毫秒）；
    - 必须 **> 0**。

    为什么把无效行**排除**而不是当 0：
    `duration_ms` 缺失的事件（老版本客户端、或上报失败）如果当 0 算进去，
    P50 会被拉成"启动 0ms" —— 那是一个**假的乐观结论**。
    """
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("duration_ms")
    # bool 是 int 的子类 ⇒ 必须先排除，否则 True 会被当成 1ms。
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value <= 0:
        return None
    return value


async def _durations_in_window(
    db: AsyncSession,
    event_type: str,
    start: datetime,
    end: datetime,
) -> list[float]:
    """窗口内该事件的有效 `duration_ms` 列表。"""
    rows = await db.execute(
        select(Feedback.metadata_).where(
            Feedback.type == event_type,
            Feedback.created_at >= start,
            Feedback.created_at < end,
        )
    )
    out: list[float] = []
    for (metadata,) in rows:
        value = _extract_duration_ms(metadata)
        if value is not None:
            out.append(value)
    return out


def _metric_block(
    event: str,
    durations: Sequence[float],
    target_ms: int,
) -> dict[str, Any]:
    """一个指标的呈现块（口径全在这一处，admin_router 只负责取数）。"""
    return {
        "event": event,
        "samples": len(durations),
        # 没有样本 ⇒ None（不是 0）：看板必须能区分「没数据」和「0ms」
        "p50_ms": quantile(durations, 0.50),
        "p95_ms": quantile(durations, 0.95),
        "target_ms": target_ms,
    }


async def client_perf_stats(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """客户端性能指标（最近 N 天）。

    Returns:
        {
          "window_days": 7,
          "window_start": "2026-10-19",   # 含
          "window_end":   "2026-10-26",   # 不含
          "first_frame": {
            "event": "app_first_frame",
            "samples": 12,
            "p50_ms": 1234.0,      # 没有样本时是 None
            "p95_ms": 2100.0,
            "target_ms": 1500,
          },
          "audio_start": {
            "event": "audio_play_start_client",
            "samples": 8,
            "p50_ms": 380.0,
            "p95_ms": 900.0,
            "target_ms": 500,
          },
        }
    """
    # ⚠️ 窗口上界必须取**库里的钟**（`db_now`），不能用 `datetime.now(timezone.utc)`：
    # 库里 `created_at` 存的是 PG session 时区（生产 Asia/Shanghai）的**墙上时间**，
    # 而 UTC now 比它慢 8 小时 ⇒ 最近 8 小时的样本会被漏掉（实测 samples=0）。
    # 详见 `common/timebase.py` 的模块说明。
    naive_now = await db_now(db, now=now)
    start = naive_now - timedelta(days=window_days)

    first_frame = await _durations_in_window(db, EventName.APP_FIRST_FRAME.value, start, naive_now)
    audio_start = await _durations_in_window(
        db, EventName.AUDIO_PLAY_START_CLIENT.value, start, naive_now
    )

    return {
        "window_days": window_days,
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": naive_now.strftime("%Y-%m-%d"),
        "first_frame": _metric_block(
            EventName.APP_FIRST_FRAME.value, first_frame, TARGET_FIRST_FRAME_MS
        ),
        "audio_start": _metric_block(
            EventName.AUDIO_PLAY_START_CLIENT.value, audio_start, TARGET_AUDIO_START_MS
        ),
    }
