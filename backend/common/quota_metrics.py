"""quota 专属 Prometheus metrics（CP3.5）。

7 个 metric：
  - quota_consume_total         Counter（plan label）
  - quota_consume_blocked_total Counter（reason label）
  - quota_consume_duration_seconds Histogram
  - quota_refund_total          Counter（trigger label）
  - quota_cache_hit_total       Counter
  - quota_cache_miss_total      Counter
  - quota_reset_total           Counter
"""
from prometheus_client import Counter, Histogram

quota_consume_total = Counter(
    "quota_consume_total",
    "quota 扣减成功次数",
    ["plan"],  # free/student/member/pro
)
quota_consume_blocked_total = Counter(
    "quota_consume_blocked_total",
    "quota 扣减被拒绝（exceeded）次数",
    ["reason"],  # exceeded/conflict
)
quota_consume_duration_seconds = Histogram(
    "quota_consume_duration_seconds",
    "quota 扣减耗时（含 cache + db）",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
quota_refund_total = Counter(
    "quota_refund_total",
    "quota 退还次数",
    ["trigger"],  # distill_failed/admin/distill_step_error
)
quota_cache_hit_total = Counter(
    "quota_cache_hit_total",
    "quota cache 命中次数",
)
quota_cache_miss_total = Counter(
    "quota_cache_miss_total",
    "quota cache miss 次数（走 DB）",
)
quota_reset_total = Counter(
    "quota_reset_total",
    "quota 月度重置次数",
)
quota_request_total = Counter(
    "quota_request_total",
    "quota API 请求次数",
    ["endpoint"],  # me_quota
)
