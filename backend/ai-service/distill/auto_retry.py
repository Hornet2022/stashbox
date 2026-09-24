"""CP3.7.3 §2.2.E：AutoRetryHook（评分 < 3 → 重新入队）。

按 docs/听感产品化方案_v1.md §2.2.E + §2.8 风险清单实现：
- 评分 < 3 → 触发自动重蒸
- 单用户 1 天最多 3 次（限流，防自动重蒸风暴）
- 关闭开关：AUTO_RETRY_ENABLED=false

CP3.7.3：本期只搭骨架（不真重入队），CP3.7.x 上线时再接 arq.enqueue_job。
"""

from __future__ import annotations

import os
import structlog

log = structlog.get_logger("distill.auto_retry")

# 触发门槛：overall_score <= 2（与 docs §2.1.A check constraint 一致）
_TRIGGER_SCORE_THRESHOLD = 2.0
# 限流：单用户 1 天最多 3 次
_DAILY_LIMIT = 3


def _is_enabled() -> bool:
    """CP3.7.3 §2.6：env 关闭开关（默认开启）。"""
    return os.getenv("AUTO_RETRY_ENABLED", "true").lower() not in ("false", "0", "no")


def should_auto_retry(overall_score: float, user_daily_retry_count: int) -> bool:
    """CP3.7.3 §2.2.E：判断是否触发自动重蒸。

    规则：
    1. AUTO_RETRY_ENABLED=false → False
    2. overall_score > 2 → False（只重蒸 < 3 的）
    3. user_daily_retry_count >= 3 → False（限流）

    Args:
        overall_score: 听感评分（1-5）
        user_daily_retry_count: 用户今日已重蒸次数

    Returns:
        True if should auto retry
    """
    if not _is_enabled():
        return False
    if overall_score > _TRIGGER_SCORE_THRESHOLD:
        return False
    if user_daily_retry_count >= _DAILY_LIMIT:
        log.warning(
            "auto_retry_rate_limit",
            overall_score=overall_score,
            daily_count=user_daily_retry_count,
            limit=_DAILY_LIMIT,
        )
        return False
    return True
