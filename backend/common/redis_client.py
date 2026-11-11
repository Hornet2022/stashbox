"""
Redis 客户端 - redis-py async。
"""
from typing import AsyncGenerator

import redis.asyncio as redis

from stashbox.backend.common.config import settings


_redis_pool: redis.ConnectionPool | None = None


def get_redis_pool() -> redis.ConnectionPool:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


def reset_redis_pool() -> None:
    """丢弃全局连接池（下次使用时按当前事件循环重建）。

    ## 为什么需要（2026-11-11）

    redis-py 的连接绑定**创建它的事件循环**。生产是单进程单事件循环 ⇒ 没问题；
    但**测试环境**里一个进程要跑几百条用例，每条用例一个新事件循环 ⇒
    池里会留下上一个循环的连接 ⇒ 在新循环里用它就抛
    「got Future attached to a different loop」/「Event loop is closed」。

    ⚠️ 报错点在业务代码里（实测是鉴权里的 `client.get`），看起来像
    「Redis 挂了」，其实只是连接属于旧循环 ⇒ 排查成本很高。

    ⇒ 测试在每条用例前调一次这个函数。
    """
    global _redis_pool
    _redis_pool = None


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI 依赖注入"""
    client = redis.Redis(connection_pool=get_redis_pool())
    try:
        yield client
    finally:
        await client.aclose()
