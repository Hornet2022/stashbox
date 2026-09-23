"""CP3.6.2：Tag vocabulary Redis 缓存。

动机：`_load_tag_vocabulary()` 每次蒸馏都查 DB 拼 tag 列表，注入到 Step 1 的 STEP1_SYSTEM prompt 里。
一篇 100 个 tag 的列表约 200-500 token，每篇都重算 = 浪费。

策略：
- Redis key: `tag_vocab:v1:{tags_last_updated_iso}` —— 用 tags 表 last_updated 当 cache key 后缀，
  tag 表变更时 key 自动变化，旧的 key 自然 TTL 过期，无需显式 invalidate。
- TTL 5 分钟兜底（即使没变更，5min 后重查 DB，避免长期 stale）。
- 失败兜底：Redis 不可用 → 退化到直接查 DB（不阻塞主流程）。
- 冷启动（DB tags 表为空） → 返回 "（暂无候选标签...）" 文案（保持旧行为）。
"""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models.tag import Tag
from stashbox.backend.common.redis_client import get_redis_pool

log = structlog.get_logger(__name__)

REDIS_KEY_PREFIX = "tag_vocab:v1"
CACHE_TTL_SECONDS = 300  # 5 分钟

# DB 表为空时的兜底文案（与 steps.py:_load_tag_vocabulary 旧行为完全一致）
_FALLBACK_EMPTY = "（暂无候选标签，请自由输出 3-5 个主题词）"
_FALLBACK_UNAVAILABLE = "（候选标签暂不可用，请自由输出 3-5 个主题词）"


def _last_updated_key(last_updated_iso: str | None) -> str:
    """拼 Redis key（包含 last_updated 后缀）。"""
    suffix = last_updated_iso or "none"
    return f"{REDIS_KEY_PREFIX}:{suffix}"


async def _get_tags_last_updated(db: AsyncSession) -> str | None:
    """读 tags 表的最新 updated_at（用于 cache key 后缀）。

    Tag 表无 updated_at 字段时返回 None（不影响主流程）。
    """
    try:
        from sqlalchemy import func

        result = await db.execute(select(func.max(Tag.created_at)))
        max_ts = result.scalar()
        return max_ts.isoformat() if max_ts else None
    except Exception:
        return None


async def _load_vocab_from_db(db: AsyncSession) -> str:
    """从 DB 直接拉 tag.name 列表（不走缓存）。失败兜底 → 退化文案。"""
    try:
        names = (await db.execute(select(Tag.name).order_by(Tag.name))).scalars().all()
        if not names:
            return _FALLBACK_EMPTY
        return "、".join(names)
    except Exception as exc:
        log.warning("tag_vocabulary_db_load_failed", error=str(exc))
        return _FALLBACK_UNAVAILABLE


async def load_tag_vocabulary(db: AsyncSession) -> str:
    """CP3.6.2 缓存版：先查 Redis → miss 查 DB → 写 Redis TTL 5min。

    Args:
        db: AsyncSession（DB session，用于 miss 时查 DB）

    Returns:
        str: tag 列表（"、"分隔）或兜底文案

    失败兜底（Redis 不可用 / DB 不可用）：
        - Redis fail: 直接查 DB（绕过缓存，不抛异常）
        - DB fail: 返回 _FALLBACK_UNAVAILABLE 文案
    """
    import redis.asyncio as redis_async

    # 1. 算 cache key（含 last_updated 后缀）
    last_updated_iso = await _get_tags_last_updated(db)
    redis_key = _last_updated_key(last_updated_iso)

    # 2. 查 Redis
    try:
        client = redis_async.Redis(connection_pool=get_redis_pool())
        cached = await client.get(redis_key)
        if cached is not None:
            text = cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)
            # 是兜底文案也照返（让 Redis 缓存所有结果，包括 fallback）
            return text
    except Exception as exc:
        log.warning("tag_vocabulary_redis_read_failed_fallback_db", error=str(exc))
        # Redis 不可用 → 直接查 DB，不阻塞
        return await _load_vocab_from_db(db)

    # 3. miss → 查 DB → 写 Redis
    vocab = await _load_vocab_from_db(db)
    try:
        await client.set(redis_key, vocab, ex=CACHE_TTL_SECONDS)
    except Exception as exc:
        log.warning("tag_vocabulary_redis_write_failed", error=str(exc))
        # 写失败不影响主流程

    return vocab
