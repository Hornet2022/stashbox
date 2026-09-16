"""蒸馏成本归因（CP3.5-pre-4，v1 §10.6）。

按 model / user / article 三维度把 LLM 花费聚到 Redis hash
（承宇 2026-09-16 决策：用 Redis hash + TTL 而非 PG 表 —— 明细自动过期，PG 只存长期汇总）。

Redis key：
- `cost:user:{user_id}:{date}`     TTL 30 天 —— 谁花得多
- `cost:article:{article_id}`      TTL 7 天  —— 哪类文章贵
- `cost:global:{date}`             TTL 90 天 —— 全局按 model 汇总 + total_requests

本模块只被 Langfuse 启用时调用（见 llm/base.py::_maybe_trace）——
默认 disable 模式下不连 Redis、无副作用（任务包 §4.4）。
"""
import logging
import os
from datetime import date
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# 美元 / 1K tokens，按 2026-09 单价
COST_PER_1K_TOKENS = {
    "claude-4-sonnet-20250514": {"input": 0.003, "output": 0.015},
    "qwen2.5-vl-72b-instruct": {"input": 0.002, "output": 0.006},
    "mock-model": {"input": 0.0, "output": 0.0},
}

USER_TTL_SEC = 86400 * 30
ARTICLE_TTL_SEC = 86400 * 7
GLOBAL_TTL_SEC = 86400 * 90


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按 model 单价算一次调用的 USD 成本（未知 model 记 0，不报错）。"""
    rates = COST_PER_1K_TOKENS.get(model, {"input": 0.0, "output": 0.0})
    return (prompt_tokens / 1000) * rates["input"] + (completion_tokens / 1000) * rates["output"]


class CostTracker:
    """成本追踪器（Redis hash + atomic HINCRBYFLOAT）。

    Args:
        client: 注入 redis client（单测用假 client；不传则按 REDIS_URL 自建）
    """

    def __init__(self, client: Any = None, redis_url: str | None = None):
        self._client = client
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._owns_client = client is None

    def _new_client(self):
        pool = redis.ConnectionPool.from_url(self._redis_url, decode_responses=True)
        return redis.Redis(connection_pool=pool)

    async def record_llm_usage(
        self,
        user_id: int,
        article_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """记录一次 LLM 调用的成本，返回本次 USD 金额。

        写 Redis 失败只 log 不抛 —— 归因失败不能让蒸馏挂掉（任务包 §8）。
        """
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        today = date.today().isoformat()
        user_key = f"cost:user:{user_id}:{today}"
        article_key = f"cost:article:{article_id}"
        global_key = f"cost:global:{today}"

        client = self._client or self._new_client()
        try:
            pipe = client.pipeline()
            pipe.hincrbyfloat(user_key, model, cost)
            pipe.hincrbyfloat(article_key, model, cost)
            pipe.hincrbyfloat(global_key, model, cost)
            pipe.hincrby(global_key, "total_requests", 1)
            pipe.expire(user_key, USER_TTL_SEC)
            pipe.expire(article_key, ARTICLE_TTL_SEC)
            pipe.expire(global_key, GLOBAL_TTL_SEC)
            await pipe.execute()
        except Exception as exc:
            logger.warning("cost tracker 写 redis 失败（忽略）: %s", exc)
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except Exception:  # 关连接失败无所谓
                    pass

        return cost
