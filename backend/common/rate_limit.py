"""Redis 固定窗口限流。

为什么需要（2026-10-21）

`POST /api/v1/auth/wechat-login` 是**未鉴权**端点，而当前实现是
「code 派生 open_id，查不到就建号」（user-service/main.py）。两者相加：

  - 任意 code 都能建一行 ``users`` —— 无界的建号向量
  - 每建一个号就带一份免费配额 —— 刷号即刷免费蒸馏额度
    （蒸馏要真花 LLM + TTS 的钱）
  - 内测一开放公网网关，这是最容易被顺手利用的一个口子

为什么不用现成方案：仓库里没有 slowapi / fastapi-limiter。为了一个端点
引入新依赖不划算，而固定窗口 + Redis 原子计数 30 行就够。

设计取舍
--------
- **固定窗口**而非滑动窗口：实现简单、原子性好。代价是窗口边界上最多能放行
  2 倍配额量（先在窗口末尾打满，再在下一个窗口开头打满）。对一个"防刷号"
  用途的限流，这个量级完全可接受。
- **Redis 挂了 → 放行（fail-open）**：限流是保护措施，不是业务主路径。
  Redis 故障时把所有人挡在门外，比放行更糟（内测期间全量用户登不进去）。
  但要在日志里留下 warn，不能默默当作限流生效了。
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as redis
from redis.exceptions import NoScriptError, RedisError

from stashbox.backend.common.redis_client import get_redis_pool

log = logging.getLogger(__name__)


# 原子计数 + 首次设置过期时间。
#
# 不能拆成 INCR / EXPIRE 两条命令：中间崩了就会留下一个永不过期的 key，
# 那个 IP 此后被永久限流（比不限流还糟）。
#
# TTL 为 -1 说明 key 存在但没有过期时间（异常残留），顺手补上。
_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 or redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then ttl = tonumber(ARGV[1]) end
return {c, ttl}
"""


def _client() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


async def _eval(client: redis.Redis, script: str, keys: list[str], args: list[Any]) -> Any:
    """EVALSHA，失败（脚本未加载）时回退 EVAL。"""
    try:
        return await client.evalsha(client.register_script(script).sha, len(keys), *keys, *args)
    except NoScriptError:
        return await client.eval(script, len(keys), *keys, *args)


async def check(
    key: str,
    *,
    limit: int,
    window_sec: int,
) -> tuple[bool, int]:
    """记一次命中，返回 ``(是否放行, 窗口内剩余次数)``。

    - ``key`` 应当已经包含身份维度（如 IP），由调用方拼好
    - ``limit <= 0`` 表示不限流（直接放行）
    - Redis 不可用时**放行**，并打 warn —— 见模块 docstring 的取舍说明
    """
    if limit <= 0:
        return True, limit

    client = _client()
    try:
        count, _ttl = await _eval(client, _LUA, [key], [int(window_sec)])
    except RedisError as exc:
        log.warning("ratelimit_check_failed: key=%s err=%s（fail-open 放行）", key, exc)
        return True, limit
    finally:
        await client.aclose()

    count = int(count)
    if count > limit:
        return False, 0
    return True, limit - count


async def reset(key: str) -> None:
    """清掉某个 key 的计数（管理员手工解除误伤 / 测试用）。"""
    client = _client()
    try:
        await client.delete(key)
    except RedisError as exc:
        log.warning("ratelimit_reset_failed: key=%s err=%s", key, exc)
    finally:
        await client.aclose()
