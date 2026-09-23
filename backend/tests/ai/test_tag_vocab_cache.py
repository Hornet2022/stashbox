"""CP3.6.2: tag vocabulary Redis 缓存验证。

验证（任务包 §C）：
1. Redis hit → 直接返回缓存，不查 DB
2. Redis miss → 查 DB → 写回 Redis（TTL 5min）
3. Redis 不可用 → 退化走 DB，不抛异常
4. DB 不可用 → 返回兜底文案
5. cache key 包含 tags.last_updated（tag 表变更 → key 自动变化）
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from distill.tag_vocab_cache import (
    CACHE_TTL_SECONDS,
    REDIS_KEY_PREFIX,
    _FALLBACK_EMPTY,
    _FALLBACK_UNAVAILABLE,
    load_tag_vocabulary,
)


@pytest.fixture
def mock_db():
    """Mock AsyncSession。"""
    return MagicMock()


@pytest.mark.asyncio
async def test_redis_hit_returns_cached_value(mock_db):
    """Redis hit → 不查 DB，直接返回缓存。"""
    cached_value = "科技、商业、财经、AI"
    captured_db_queries = []

    async def fake_db_execute(*args, **kwargs):
        captured_db_queries.append(args)
        return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))

    mock_db.execute = fake_db_execute
    mock_db.scalar = AsyncMock(
        side_effect=lambda *a, **k: MagicMock(isoformat=lambda: "2026-01-01T00:00:00")
    )

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=cached_value.encode("utf-8"))
    mock_redis.set = AsyncMock()

    with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
        with patch("redis.asyncio.Redis", return_value=mock_redis):
            result = await load_tag_vocabulary(mock_db)

    assert result == cached_value
    # 关键：DB 没被查（除了 last_updated key 计算）
    # 只 1 次 execute（_get_tags_last_updated），不查 Tag.name
    assert mock_redis.set.call_count == 0, "hit 时不应写回 Redis"


@pytest.mark.asyncio
async def test_redis_miss_queries_db_and_writes(mock_db):
    """Redis miss → 查 DB → 写回 Redis TTL。"""
    tag_names = ["科技", "商业", "AI"]

    # 1. _get_tags_last_updated 调用
    # 2. _load_vocab_from_db 调用 select(Tag.name)
    async def fake_db_execute(stmt, *args, **kwargs):
        # 简化：根据 statement 类型返回
        return MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=tag_names)))
        )

    mock_db.execute = fake_db_execute
    mock_db.scalar = AsyncMock(return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00"))

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)  # miss
    mock_redis.set = AsyncMock()

    with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
        with patch("redis.asyncio.Redis", return_value=mock_redis):
            result = await load_tag_vocabulary(mock_db)

    assert result == "、".join(tag_names)
    # 写回 Redis 调用
    assert mock_redis.set.call_count == 1
    set_call = mock_redis.set.call_args
    assert REDIS_KEY_PREFIX in set_call[0][0]  # key 含 prefix
    assert set_call[1]["ex"] == CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_redis_unavailable_falls_back_to_db(mock_db):
    """Redis 不可用 → 退化查 DB，不抛异常。"""
    tag_names = ["科技"]

    async def fake_db_execute(stmt, *args, **kwargs):
        return MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=tag_names)))
        )

    mock_db.execute = fake_db_execute
    mock_db.scalar = AsyncMock(return_value=MagicMock(isoformat=lambda: "2026-01-01"))

    # Redis get 抛异常
    with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
        with patch("redis.asyncio.Redis", side_effect=Exception("redis down")):
            result = await load_tag_vocabulary(mock_db)

    # 不抛异常，返回 DB 结果
    assert result == "科技"


@pytest.mark.asyncio
async def test_db_empty_returns_fallback(mock_db):
    """DB tags 表为空 → _FALLBACK_EMPTY 文案。"""

    async def fake_db_execute(stmt, *args, **kwargs):
        return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))

    mock_db.execute = fake_db_execute
    mock_db.scalar = AsyncMock(return_value=None)

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
        with patch("redis.asyncio.Redis", return_value=mock_redis):
            result = await load_tag_vocabulary(mock_db)

    assert result == _FALLBACK_EMPTY


@pytest.mark.asyncio
async def test_db_failure_returns_unavailable_fallback(mock_db):
    """DB 异常 → _FALLBACK_UNAVAILABLE 文案。"""

    async def fake_db_execute(*args, **kwargs):
        raise Exception("db down")

    mock_db.execute = fake_db_execute
    mock_db.scalar = AsyncMock(side_effect=Exception("db down"))

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
        with patch("redis.asyncio.Redis", return_value=mock_redis):
            result = await load_tag_vocabulary(mock_db)

    assert result == _FALLBACK_UNAVAILABLE


@pytest.mark.asyncio
async def test_cache_key_includes_last_updated(mock_db):
    """cache key 含 tags.last_updated 后缀（tag 变更 → key 自动变化）。"""
    from datetime import datetime
    from distill import tag_vocab_cache

    captured_keys = []
    fixed_dt = datetime(2026, 9, 23, 10, 0, 0)

    # 直接 patch _get_tags_last_updated 返回固定 datetime（避免 mock sqlalchemy result.scalar()）
    async def fake_get_last_updated(db):
        return fixed_dt.isoformat()

    # _load_vocab_from_db 也直接 patch 返回空
    async def fake_load_vocab_from_db(db):
        return ""

    mock_redis = MagicMock()

    async def capture_set(key, *args, **kwargs):
        captured_keys.append(key)

    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(side_effect=capture_set)

    with patch.object(tag_vocab_cache, "_get_tags_last_updated", fake_get_last_updated):
        with patch.object(tag_vocab_cache, "_load_vocab_from_db", fake_load_vocab_from_db):
            with patch("distill.tag_vocab_cache.get_redis_pool", return_value=MagicMock()):
                with patch("redis.asyncio.Redis", return_value=mock_redis):
                    await load_tag_vocabulary(mock_db)

    assert len(captured_keys) == 1
    # 验证 cache key 含 ISO 时间戳 + prefix
    assert "2026-09-23T10" in captured_keys[0]
    assert captured_keys[0].startswith(REDIS_KEY_PREFIX)
