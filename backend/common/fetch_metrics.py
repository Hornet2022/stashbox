"""剪藏抓取指标（剪藏成功率的可观测面）。

## 为什么这个模块值得存在

剪藏成功率是听匣的生命线，但"成功率"这件事如果只在日志里，就是**不可优化**的：
线上成功率从 92% 掉到 71%，没人知道是哪一层抓取、哪个错误码、哪类站点导致的，
于是只能凭感觉改代码。而本轮改造把抓取拆成了多层降级通道
（见 `fetchers/pipeline.py`），层数越多，**归因**就越重要。

三个维度，缺一不可：

1. **命中哪一节通道**（`http` / `browser`）—— 决定还要不要投更多资源
   到浏览器通道上。若 99% 都走 http，买代理的钱应该花在别处。
2. **失败在哪个错误码**（`fetcher.parse` / `fetcher.auth` ...）—— 决定改什么。
   `parse` 高说明 JS 渲染页多；`auth` 高说明被风控，该上代理。
3. **升级到下一通道的次数** —— 衡量"降级链到底救回多少"。

⚠️ 刻意**不**把 URL / 域名放进 label：剪藏是用户提交任意链接，
按域名打标签会让基数无界（Prometheus 内存被打爆），而且等于把用户浏览历史
写进指标系统。域名维度要看就去日志里按 `fetch_ok` 查。

## 为什么用 Counter 而不是 Histogram 的成功率比值

不提供 `success_ratio` 这类派生指标：比率型指标在分母很小时噪声极大
（抓 1 次失败和抓 100 次失败 1 次是同一种"100% 失败"），
而 Prometheus 侧用 `rate()` 在两个 Counter 上算比值更准。
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

#: 一次抓取最终落在哪一节通道 / 根本没成功
fetch_attempts_total = Counter(
    "stashbox_fetch_attempts_total",
    "剪藏抓取结果，按命中通道与最终错误码统计",
    ["tier", "outcome"],
)

#: 降级链升级次数（Tier N 失败 → 试 Tier N+1）
fetch_escalations_total = Counter(
    "stashbox_fetch_escalations_total",
    "抓取降级链升级次数，按失败所在通道与错误码统计",
    ["from_tier", "code"],
)

#: 整条抓取耗时（含降级链）
fetch_duration_seconds = Histogram(
    "stashbox_fetch_duration_seconds",
    "一次剪藏抓取的总耗时（含降级链所有通道）",
    ["tier"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 60.0),
)

#: Tier 2 浏览器通道不可用的次数（没装 playwright / 二进制没下载 / 被配置关掉）
#:
#: 刻意只记"不可用"不记"可用"：rate() 为 0 就说明通道健康。而"可用次数"
#: 这种分母会随流量涨落，看了也不知道该修什么。
#: 非 0 意味着线上**正在少一层兜底** —— 这是必须被告警的静默降级。
fetch_browser_tier_unavailable_total = Counter(
    "stashbox_fetch_browser_tier_unavailable_total",
    "浏览器兜底通道不可用次数（非 0 = 线上正在少一层抓取兜底）",
)


def record_success(tier: str, duration: float) -> None:
    fetch_attempts_total.labels(tier=tier, outcome="success").inc()
    fetch_duration_seconds.labels(tier=tier).observe(duration)


def record_failure(tier: str, code: str, duration: float) -> None:
    fetch_attempts_total.labels(tier=tier, outcome=f"fail:{code}").inc()
    fetch_duration_seconds.labels(tier=tier).observe(duration)


def record_escalation(from_tier: str, code: str) -> None:
    fetch_escalations_total.labels(from_tier=from_tier, code=code).inc()


__all__ = [
    "fetch_attempts_total",
    "fetch_browser_tier_unavailable_total",
    "fetch_duration_seconds",
    "fetch_escalations_total",
    "record_escalation",
    "record_failure",
    "record_success",
]
