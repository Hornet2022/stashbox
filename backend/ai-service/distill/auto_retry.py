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

import redis.asyncio as aioredis
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

    2026-10：原来这里（以及下面 bump）自己 `aioredis.from_url(...)` 现建连接池，
    有两个问题，都不是「性能」而是「正确性」：

    1. **漏了密码**。`settings.redis_url`（common/config.py）拼 URL 时带
       `redis://:<password>@host:port/db`，而这里手搓的
       `redis://{host}:{port}/{db}` 不带。Redis 一旦启用密码，这两个函数
       每一次都认证失败 —— 而 except 把失败吞成 `return 0`。后果是**限流
       完全失效**：读永远是 0（< 3 → 永远放行），写也永远是 0（计数不动），
       且日志只有一条 warning。
    2. **每次调用新建一个连接池**再 aclose 掉。一次重试要建两次池，
       高频调用下是纯粹的握手开销，且与非并发安全的自建池生命周期纠缠。

    现在统一走 `common.redis_client.get_redis_pool()`（进程级共享池）。
    借来的池**不需要也不能**在这里关：命令执行时向池借一条连接、用完立即归还，
    薄客户端本身不持有连接；而 `Redis(connection_pool=...)` 的
    `auto_close_connection_pool` 是 False，真去 aclose 也断不掉共享池 ——
    两点都已对着 redis-py 5.3.1 的源码和实际行为确认过（from_url 路径才是 True，
    那正是旧代码每次新建再关掉自己那个池的机制）。
    """
    from stashbox.backend.common.redis_client import get_redis_pool

    try:
        client = aioredis.Redis(connection_pool=get_redis_pool())
        val = await client.get(_RETRY_COUNT_KEY.format(user_id=user_id))
        return int(val) if val else 0
    except Exception as exc:
        log.warning("auto_retry_count_read_failed", user_id=user_id, error=str(exc))
        return 0


async def bump_user_daily_retry_count(user_id: int) -> int:
    """计数 +1，返回当天累计次数。计数器带 24h 过期。

    连接池与鉴权的问题同 `get_user_daily_retry_count`，见那里的说明。
    """
    from stashbox.backend.common.redis_client import get_redis_pool

    try:
        client = aioredis.Redis(connection_pool=get_redis_pool())
        key = _RETRY_COUNT_KEY.format(user_id=user_id)
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 24 * 3600)
        return int(count)
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

    2026-10 修 P0：原来这里写的是 `from .dispatcher import Dispatcher`。
    `distill/` 下**没有** dispatcher.py —— dispatcher 在 `ai-service/dispatcher.py`
    （类名是 `DistillDispatcher`）。这个 ImportError 被下面的 `except Exception`
    吞掉，只留一行 `auto_retry_enqueue_failed` 日志。

    而调用方 hooks_impl.py:424 的 `bump_user_daily_retry_count()` 在**调用之前**
    就已经执行了（那里的注释明确写着「先占额度再入队」）。两者相加的后果是：

        用户打 1-2 星 → 扣掉一次重试额度 → 入队失败被吞 → 什么都没发生

    功能等于不存在，用户还白白损失每天 3 次里的额度。评分闭环在体验上是断的，
    而 `auto_retry_triggered` 那条 info 日志还让人以为它跑通了。
    """
    import uuid

    try:
        # dispatcher 在 ai-service/ 下、是 distill/ 的**兄弟**而非子模块，
        # 所以不能用相对导入。用顶层名（ai-service 目录已由 main.py 加进 sys.path）。
        from dispatcher import DistillDispatcher

        dispatcher = DistillDispatcher()
        job_id = await dispatcher.enqueue_distill(
            task_id=f"retry_{uuid.uuid4().hex[:24]}",
            article_id=article_id,
            user_id=user_id,
            url=url,
            title=title,
            # 重试是给差评的补偿重跑，**不扣用户配额**（真正的闸门是每天 3 次限流）。
            # 既然没扣，失败时就也不该退 —— 见 distill_task 里 quota_charged 的说明。
            quota_charged=False,
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
