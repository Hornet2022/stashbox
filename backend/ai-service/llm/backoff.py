"""LLM 请求退避策略（动作 3，2026-09-24）。

背景
----
原实现是「429 直接抛 RateLimitError、不重试」—— 对**瞬时并发限流**极不友好：
一次 429 就让整条蒸馏失败（后续虽由 Arq 重试整个任务，但间隔长、且重复消耗
LLM 配额与前面的步骤）。

2026-09-24 实测印证这是瞬时限流而非配额耗尽：同一时刻**单发** `doubao-seed-2.0-pro`
3/3 全部 200，但**并发**（我的重派 + 其他用户任务 + Arq 重试同时打）就会 429。

策略
----
- 一般错误（超时 / 传输 / 5xx）：指数退避 `BASE * 2^attempt`
- 429：更长的退避基数，且**尊重服务端 `Retry-After`**（取较大值）
- 叠加 ±25% 抖动 → 避免多个并发任务同时重试再次互撞
- 单次等待有上限（cap），避免退避过久撑爆 Arq 的 job_timeout
"""

from __future__ import annotations

import random

BACKOFF_BASE_SEC = 2.0  # 一般错误基数
BACKOFF_BASE_429_SEC = 4.0  # 限流基数（等更久）
BACKOFF_CAP_SEC = 60.0  # 单次等待上限
BACKOFF_JITTER_RATIO = 0.25  # ±25% 抖动


def compute_backoff_seconds(
    attempt: int,
    *,
    is_rate_limited: bool = False,
    retry_after: str | None = None,
) -> float:
    """计算第 `attempt` 次（0 起）失败后的等待秒数。

    Args:
        attempt: 已失败次数（0 = 第一次失败后）
        is_rate_limited: 是否 429
        retry_after: 服务端 Retry-After 头的原始值（秒数字符串）

    Returns:
        建议 sleep 秒数（含抖动，非负）
    """
    base = BACKOFF_BASE_429_SEC if is_rate_limited else BACKOFF_BASE_SEC
    delay = min(BACKOFF_CAP_SEC, base * (2 ** max(0, attempt)))

    # 服务端显式要求等待更久 → 尊重它（仍受 cap 约束）
    if is_rate_limited and retry_after:
        try:
            delay = max(delay, min(BACKOFF_CAP_SEC, float(retry_after)))
        except (TypeError, ValueError):
            pass  # 非数字（如 HTTP-date）→ 忽略，用指数值

    jitter = delay * BACKOFF_JITTER_RATIO
    # cap 必须约束**最终值**（含抖动），否则 jitter 会让实际等待突破上限
    return max(0.0, min(BACKOFF_CAP_SEC, delay + random.uniform(-jitter, jitter)))
