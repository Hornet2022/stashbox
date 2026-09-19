"""system_config 表读写 + Redis 短缓存（CP7.3）。

admin-web 改运行时配置 → 写 system_config；服务侧读配置 → 优先 DB。
读的路径很热（每次 LLM 调用前都要确认配置有没有变），故加一层 Redis 缓存：

    system_config:{key} -> JSON（含 "null"，表示 DB 里没这组配置）  ttl 5s

写操作（PUT）提交后立刻 DEL 缓存 → 「改完下一次调用即生效」。
Redis 不可用时退化为直接读 DB，不阻塞主流程。
"""

import json
from datetime import datetime
from typing import Any

import redis.asyncio as redis
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import SystemConfig
from stashbox.backend.common.redis_client import get_redis_pool

log = get_logger(__name__)

CACHE_TTL_SECONDS = 5

KEY_LLM = "llm"


def _cache_key(key: str) -> str:
    return f"system_config:{key}"


def _redis() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


async def get_config(key: str) -> dict | None:
    """读一组配置。表里没有该 key 时返回 None。"""
    value, _ = await get_config_row(key)
    return value


async def get_config_row(key: str) -> tuple[dict | None, datetime | None]:
    """读一组配置 + 它的 updated_at。表里没有该 key 时返回 (None, None)。

    admin-web 配置页要在 GET 里显示「上次谁改的/什么时候改的」，所以这里连
    updated_at 一起取，避免 GET 吐一个永远是 null 的字段。
    """
    cache_key = _cache_key(key)
    try:
        raw = await _redis().get(cache_key)
    except Exception as exc:  # Redis 挂了不能拖垮调用方
        log.warning("system_config_cache_read_failed", error=str(exc))
    else:
        if raw is not None:
            cached = json.loads(raw)
            return cached["value"], _parse_iso(cached["updated_at"])

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemConfig, key)
        value = row.value if row is not None else None
        updated_at = row.updated_at if row is not None else None

    await _cache_set(cache_key, value, updated_at)
    return value, updated_at


async def set_config(key: str, value: dict, updated_by: int | None = None) -> dict:
    """UPSERT 一组配置，写完后失效缓存。返回落库后的行（含 updated_at）。"""
    stmt = insert(SystemConfig).values(key=key, value=value, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": value, "updated_by": updated_by, "updated_at": func.now()},
    ).returning(SystemConfig.value, SystemConfig.updated_at)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(stmt)).first()
        await db.commit()

    await invalidate(key)
    persisted_value, updated_at = row
    return {"key": key, "value": persisted_value, "updated_at": updated_at}


async def invalidate(key: str) -> None:
    try:
        await _redis().delete(_cache_key(key))
    except Exception as exc:
        log.warning("system_config_cache_invalidate_failed", error=str(exc))


async def _cache_set(cache_key: str, value: Any, updated_at: datetime | None) -> None:
    try:
        payload = json.dumps({"value": value, "updated_at": as_iso(updated_at)})
        await _redis().set(cache_key, payload, ex=CACHE_TTL_SECONDS)
    except Exception as exc:
        log.warning("system_config_cache_write_failed", error=str(exc))


def as_iso(updated_at: datetime | None) -> str | None:
    return updated_at.isoformat() if updated_at is not None else None


def _parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
