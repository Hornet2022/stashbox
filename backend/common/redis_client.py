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


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI 依赖注入"""
    client = redis.Redis(connection_pool=get_redis_pool())
    try:
        yield client
    finally:
        await client.aclose()
