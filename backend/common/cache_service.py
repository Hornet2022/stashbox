"""
Redis 缓存层 - 用户配额 / 文章详情 / 待听列表。

设计（CP1.6）：
  - user:quota:{user_id}    -> JSON {quota_used, monthly_quota, version, reset_at}  ttl 60s
  - article:detail:{id}     -> JSON                                                 ttl 300s
  - user:pending:{user_id}  -> JSON list                                            ttl 60s

原子性：配额扣减后必须失效缓存，否则会出现「DB 已扣 + 缓存旧值」。
单靠 `DEL` 不够——扣减与回填之间存在竞态：
    扣减线程: UPDATE(version 5) -> DEL
    读线程  : GET miss -> SELECT(version 4) -> SET version=4   ← 旧值复活
故用两段 Lua（EVALSHA）配合 **版本号栅栏**（`{key}:inv`）：
  1. `invalidate_quota` : 版本 >= 新版本才保留缓存，否则 DEL + 写栅栏（带新版本号）
  2. `set_quota`        : 若栅栏版本 >= 待写版本，拒绝写入（防止旧值回填覆盖）
两次 EVAL 在 Redis 内单线程执行，配合版本号比较即构成 CAS。
"""
import json
from typing import Any

import redis.asyncio as redis
from redis.exceptions import NoScriptError

from stashbox.backend.common.redis_client import get_redis_pool

QUOTA_TTL = 60
ARTICLE_TTL = 300
PENDING_TTL = 60
_FENCE_TTL = 120  # 栅栏存活时间 > 缓存 ttl，避免栅栏先过期

# KEYS[1]=cache key  KEYS[2]=fence key  ARGV[1]=new version
_INVALIDATE_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur then
  local ok, data = pcall(cjson.decode, cur)
  if ok and data and data['version'] and tonumber(data['version']) >= tonumber(ARGV[1]) then
    return 0
  end
end
redis.call('DEL', KEYS[1])
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[2]))
return 1
"""

# KEYS[1]=cache key  KEYS[2]=fence key  ARGV[1]=version  ARGV[2]=payload  ARGV[3]=ttl
_SET_LUA = """
local fence = redis.call('GET', KEYS[2])
if fence and tonumber(fence) > tonumber(ARGV[1]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""


def quota_key(user_id: int) -> str:
    return f"user:quota:{user_id}"


def article_key(article_id: str) -> str:
    return f"article:detail:{article_id}"


def pending_key(user_id: int) -> str:
    return f"user:pending:{user_id}"


def article_quota_key(article_id: str) -> str:
    """文章已扣配额标记（防止 POST /articles 与 /distill 重复扣）。"""
    return f"article:quota:{article_id}"


def _client() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


def _sha(client: redis.Redis, script: str) -> str:
    """返回脚本 sha（redis-py 5 的 register_script 返回 Script 对象，这里手动取 sha）。"""
    return client.register_script(script).sha


async def _eval(client: redis.Redis, script: str, keys: list[str], args: list[Any]) -> Any:
    """EVALSHA，失败（脚本未加载）时回退 EVAL。"""
    try:
        return await client.evalsha(_sha(client, script), len(keys), *keys, *args)
    except NoScriptError:
        return await client.eval(script, len(keys), *keys, *args)


# ---------------------------------------------------------------------------
# 用户配额
# ---------------------------------------------------------------------------
async def get_quota(user_id: int) -> dict | None:
    client = _client()
    try:
        raw = await client.get(quota_key(user_id))
    finally:
        await client.aclose()
    return json.loads(raw) if raw else None


async def set_quota(user_id: int, payload: dict) -> bool:
    """回填配额缓存；若已被更高版本失效（栅栏）则拒绝写入。"""
    client = _client()
    try:
        res = await _eval(
            client,
            _SET_LUA,
            [quota_key(user_id), quota_key(user_id) + ":inv"],
            [int(payload.get("version", 0)), json.dumps(payload), QUOTA_TTL],
        )
    finally:
        await client.aclose()
    return bool(res)


async def invalidate_quota(user_id: int, new_version: int) -> bool:
    """配额变更后失效缓存（Lua 原子：DEL + 写版本号栅栏）。"""
    client = _client()
    try:
        res = await _eval(
            client,
            _INVALIDATE_LUA,
            [quota_key(user_id), quota_key(user_id) + ":inv"],
            [int(new_version), _FENCE_TTL],
        )
    finally:
        await client.aclose()
    return bool(res)


# ---------------------------------------------------------------------------
# 文章详情
# ---------------------------------------------------------------------------
async def get_article(article_id: str) -> dict | None:
    client = _client()
    try:
        raw = await client.get(article_key(article_id))
    finally:
        await client.aclose()
    return json.loads(raw) if raw else None


async def set_article(article_id: str, payload: dict) -> None:
    client = _client()
    try:
        await client.set(article_key(article_id), json.dumps(payload), ex=ARTICLE_TTL)
    finally:
        await client.aclose()


async def invalidate_article(article_id: str) -> None:
    client = _client()
    try:
        await client.delete(article_key(article_id))
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 待听列表
# ---------------------------------------------------------------------------
async def get_pending(user_id: int) -> list | None:
    client = _client()
    try:
        raw = await client.get(pending_key(user_id))
    finally:
        await client.aclose()
    return json.loads(raw) if raw else None


async def set_pending(user_id: int, payload: list) -> None:
    client = _client()
    try:
        await client.set(pending_key(user_id), json.dumps(payload), ex=PENDING_TTL)
    finally:
        await client.aclose()


async def invalidate_pending(user_id: int) -> None:
    client = _client()
    try:
        await client.delete(pending_key(user_id))
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 文章「已扣配额」标记（24h）：content-service 提交时打标，ai-service 蒸馏前查
# ---------------------------------------------------------------------------
async def mark_article_quota(article_id: str) -> None:
    client = _client()
    try:
        await client.set(article_quota_key(article_id), "1", ex=86400)
    finally:
        await client.aclose()


async def has_article_quota(article_id: str) -> bool:
    client = _client()
    try:
        return bool(await client.exists(article_quota_key(article_id)))
    finally:
        await client.aclose()


async def clear_article_quota(article_id: str) -> None:
    client = _client()
    try:
        await client.delete(article_quota_key(article_id))
    finally:
        await client.aclose()
