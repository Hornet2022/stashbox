"""可观测性（CP3.5-pre-4）。

- `langfuse_client`：Langfuse 客户端封装（默认 disable）
- `decorators`：`@trace_llm_call` / `@trace_distill_step`
- `cost_tracker`：蒸馏成本归因（Redis hash）

模块同 llm / distill，都是 ai-service 目录下的顶层模块（目录名带连字符，
不是合法包名），靠 cwd / PYTHONPATH 含 ai-service 目录来 import。
"""
from .cost_tracker import COST_PER_1K_TOKENS, CostTracker, estimate_cost_usd
from .decorators import trace_distill_step, trace_llm_call
from .langfuse_client import LangfuseClient

__all__ = [
    "COST_PER_1K_TOKENS",
    "CostTracker",
    "LangfuseClient",
    "estimate_cost_usd",
    "trace_distill_step",
    "trace_llm_call",
]
