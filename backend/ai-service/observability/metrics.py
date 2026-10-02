"""蒸馏 Prometheus metrics（CP3.6）。

按 v1 §10.5 规则所需：
- DistillTaskBacklog → distill_queue_size gauge
- LLMCostSpike → llm_cost_usd_total
- 蒸馏成功率/失败率 → distill_success_total / distill_failure_total
- 蒸馏 4 步耗时 → distill_step_duration_seconds histogram
"""

from prometheus_client import Counter, Gauge, Histogram

# 蒸馏 step 耗时（v1 §11.3 验收 P95 < 5min = 300s）
#
# 桶上界不能拍脑袋定在 600s：实测本机 mlx-audio 单路 TTS 约 745s/篇
# （step3_tts 6 次采样 sum=4469s），5/6 落进 +Inf，量程形同虚设。
# 扩到 3600s 留足余量；若 TTS 再变慢，解析侧会对超量程的分位数返回 null
# 而不是编造数字（见 content-service/admin_router.py::_parse_distill_p95）。
DISTILL_STEP_DURATION = Histogram(
    "distill_step_duration_seconds",
    "Distill step latency (CP3.6, v1 §11.3)",
    ["step"],
    buckets=(
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        120.0,
        300.0,
        600.0,
        900.0,
        1200.0,
        1800.0,
        3600.0,
    ),
)

# 蒸馏尝试次数（per step）
DISTILL_ATTEMPT_TOTAL = Counter(
    "distill_attempt_total",
    "Distill step attempts (per step)",
    ["step"],
)

# 蒸馏成功次数（per step）
DISTILL_SUCCESS_TOTAL = Counter(
    "distill_success_total",
    "Distill step successes (per step)",
    ["step"],
)

# 蒸馏失败次数（per step + reason）
DISTILL_FAILURE_TOTAL = Counter(
    "distill_failure_total",
    "Distill step failures (per step + reason)",
    ["step", "reason"],
)

# LLM 调用成本（per model，美元）
LLM_COST_USD_TOTAL = Counter(
    "llm_cost_usd_total",
    "LLM call cost in USD (per model)",
    ["model"],
)

# Arq 队列长度（per queue）
DISTILL_QUEUE_SIZE = Gauge(
    "distill_queue_size",
    "Arq queue length (per queue)",
    ["queue"],
)

# CP3.6.3：TTS 并行合成总耗时（per provider + per concurrent 度）
TTS_PARALLEL_DURATION = Histogram(
    "tts_parallel_duration_seconds",
    "TTS parallel synthesis duration (per provider + concurrent, CP3.6.3)",
    ["provider", "concurrent"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0),
)
