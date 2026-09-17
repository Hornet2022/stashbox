"""stashbox.backend.common - 听匣后端共享库"""
__version__ = "0.1.0"

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
