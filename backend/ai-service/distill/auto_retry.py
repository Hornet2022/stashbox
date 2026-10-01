"""CP3.7.3 §2.2.E：AutoRetryHook（评分 < 3 → 真正重新入队）。

按 docs/听感产品化方案_v1.md §2.2.E + §2.8 风险清单实现：
- 评分 < 3 → 触发自动重蒸
- 单用户 1 天最多 3 次（Redis 计数，防自动重蒸风暴）
- 关闭开关：AUTO_RETRY_ENABLED=false

2026-10-02：从「只判断 + 打日志」升级为**真入队**。此前 hook 命中后只写一行
`log.info("auto_retry_triggered")`，用户打 1-2 星后什么都不会发生，评分闭环
在体验上完全断裂（方案 §1 闭环 1 承诺的核心动作之一）。
"""

from __future__ import annotations

import os
import structlog

log = structlog.get_logger("distill.auto_retry")

# 触发门槛：overall_score <= 2（与 docs §2.1.A check constraint 一致）
_TRIGGER_SCORE_THRESHOLD = 2.0
# 限流：单用户 1 天最多 3 次
_DAILY_LIMIT = 3
# 重蒸时用的模型档位：低分说明上一版没写好，强制升到 full 重来
_RETRY_TARGET_TIER = "full"

_RETRY_COUNT_KEY = "stashbox:auto_retry:count:{user_id}"


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


async def get_user_daily_retry_count(user_id: int) -> int:
    """读该用户今天的自动重蒸次数（Redis 计数器，读不到按 0 处理）。

    之前 hook 里是 `user_daily_retry_count = 0` 写死，限流形同虚设 ——
    真接上入队后不配限流，一次低分就能把队列打爆。
    """
    try:
        from stashbox.backend.common.config import settings

        import redis.asyncio as aioredis

        client = aioredis.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
        )
        try:
            val = await client.get(_RETRY_COUNT_KEY.format(user_id=user_id))
            return int(val) if val else 0
        finally:
            await client.aclose()
    except Exception as exc:
        log.warning("auto_retry_count_read_failed", user_id=user_id, error=str(exc))
        return 0


async def bump_user_daily_retry_count(user_id: int) -> int:
    """计数 +1，返回当天累计次数。计数器带 24h 过期。"""
    try:
        from stashbox.backend.common.config import settings

        import redis.asyncio as aioredis

        client = aioredis.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
        )
        try:
            key = _RETRY_COUNT_KEY.format(user_id=user_id)
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 24 * 3600)
            return int(count)
        finally:
            await client.aclose()
    except Exception as exc:
        log.warning("auto_retry_count_write_failed", user_id=user_id, error=str(exc))
        return 0


async def enqueue_auto_retry(
    *,
    user_id: int,
    article_id: str,
    url: str,
    title: str | None = None,
) -> str | None:
    """把文章重新丢回蒸馏队列，返回新 job_id（入队失败返回 None）。

    刻意不复用原 task_id —— Arq 用 job_id 去重，沿用旧 id 会被判为重复任务
    直接跳过。重蒸是一次全新的尝试，需要新的 id。
    """
    import uuid

    try:
        from .dispatcher import Dispatcher

        dispatcher = Dispatcher()
        job_id = await dispatcher.enqueue_distill(
            task_id=f"retry_{uuid.uuid4().hex[:24]}",
            article_id=article_id,
            user_id=user_id,
            url=url,
            title=title,
        )
        log.info(
            "auto_retry_enqueued",
            user_id=user_id,
            article_id=article_id,
            new_job_id=job_id,
            target_tier=_RETRY_TARGET_TIER,
        )
        return job_id
    except Exception as exc:
        log.warning(
            "auto_retry_enqueue_failed",
            user_id=user_id,
            article_id=article_id,
            error=str(exc),
        )
        return None
