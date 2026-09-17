"""stashbox.backend.common - 听匣后端共享库"""
__version__ = "0.1.0"

# Prometheus metrics 公共 API：经 `stashbox.backend.common` 命名空间对外暴露。
# 显式声明 __all__ 而非 noqa，避免 ruff F401 误判为 unused import 而删除。
__all__ = [
    "quota_consume_total",
    "quota_consume_blocked_total",
    "quota_consume_duration_seconds",
    "quota_refund_total",
    "quota_cache_hit_total",
    "quota_cache_miss_total",
    "quota_reset_total",
    "quota_request_total",
]

from stashbox.backend.common.quota_metrics import (
    quota_consume_total,
    quota_consume_blocked_total,
    quota_consume_duration_seconds,
    quota_refund_total,
    quota_cache_hit_total,
    quota_cache_miss_total,
    quota_reset_total,
    quota_request_total,
)
