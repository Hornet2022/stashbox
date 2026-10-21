"""token 吊销（注销账号 / 泄露后的补救）（2026-10-21）

## 为什么需要

``common/auth.py`` 的 ``require_user`` 只解 JWT，**不查库**。
这意味着"用户注销了"和"用户被封了"这两件事，在 token 层面都拦不住 ——
注销后那个 JWT 还能用满 7 天（``jwt_expire_minutes`` 的默认值）。

注销是合规承诺："注销后停止提供服务、删除个人信息"。如果旧 token 还能用，
这个承诺就是假的。

## 为什么用 Redis 而不是查库

两个选项：

1. **每次鉴权查一次 users 表** —— 最直白，但给**每个**业务请求加一次 DB 往返。
   这个仓库里 ``require_user`` 被四个服务共用，鉴权在热路径上。
2. **Redis 记一个"吊销时间点"** —— 鉴权时只多一次 Redis GET（亚毫秒），
   不用碰 PG。

选 2。

## 判据：签发时间早于吊销时间 = 已吊销

不需要给每个 token 建黑名单（那要引入 jti + 清理逻辑）。
只要记住"**这个用户在 T 时刻被吊销**"，那么：
- ``iat < T`` 的 token → 吊销前签发的 → 作废
- ``iat >= T`` 的 token → 吊销后签发的 → 有效（正常情况下不会有，
  因为注销时用户已经被软删，refresh 端点也会拒绝给他发新 token）

用 iat 判定而不是"记一个当前 epoch 计数器"，是因为后者需要改所有签发点
（``create_access_token`` 有多个调用方），而 iat 本来就在 token 里。

## 代价（明说）

- **Redis 重启会丢标记**：最坏情况下，一个已注销用户的旧 token
  在 Redis 重启后又能用，直到 access token 自然过期（7 天）。
  真要收紧，把 ``jwt_expire_minutes`` 调到 1 小时即可 ——
  那样最坏窗口从 7 天变成 1 小时，代价是 App 需要更频繁地 refresh
  （安卓端 AuthInterceptor 已经在 401 时自动 refresh，扛得住）。
- **Redis 挂了 → 放行（fail-open）**：和 `common/rate_limit.py` 同一个取舍。
  鉴权依赖 Redis 做不成 —— Redis 故障时把所有人挡在门外，比放行更糟。
  真正的"事实"仍在数据库（``users.deleted_at``），Redis 只是它的热路径缓存。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import redis.asyncio as redis
from redis.exceptions import RedisError

from stashbox.backend.common.config import settings
from stashbox.backend.common.redis_client import get_redis_pool

log = logging.getLogger(__name__)

# key 前缀。带 user_id 便于按用户清理。
_KEY_PREFIX = "auth:revoked_after:"


def _client() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}{user_id}"


async def revoke(user_id: int, *, at: float | None = None) -> None:
    """吊销该用户**已签发**的全部 token。

    记的是"吊销时刻"，判据是 token 的 ``iat`` 早于它 → 作废。
    TTL 取 access token 的有效期：超过这个时长所有旧 token 自然过期，
    标记就没有存在意义了。
    """
    ts = at if at is not None else time.time()
    ttl = max(60, settings.jwt_expire_minutes * 60)
    client = _client()
    try:
        await client.set(_key(user_id), str(ts), ex=ttl)
    except RedisError as exc:
        # 吊销标记写不进去 → 旧 token 在 TTL 内仍可用。
        # 事实仍在 DB（users.deleted_at），refresh 端点会拒绝他换新 token，
        # 所以最坏是"已注销用户的旧 access token 还能用到自然过期"。
        # 这条必须留 warn —— 静默失败等于悄悄放弃了注销的承诺。
        log.warning(
            "token_revocation_write_failed: user_id=%s err=%s（注销的即时失效未生效）",
            user_id,
            exc,
        )
    finally:
        await client.aclose()


async def is_revoked(payload: dict[str, Any]) -> bool:
    """这个 token 是否已被吊销。

    - 查不到标记 → 没被吊销（正常路径，一次 Redis GET）
    - 标记存在且 ``iat < 吊销时刻`` → 已吊销
    - Redis 不可用 → **放行**（fail-open，见模块说明）
    """
    sub = payload.get("sub")
    iat = payload.get("iat")
    if sub is None or iat is None:
        return False
    try:
        uid = int(sub)
    except (TypeError, ValueError):
        return False

    client = _client()
    try:
        raw = await client.get(_key(uid))
    except RedisError as exc:
        log.warning("token_revocation_check_failed: user_id=%s err=%s（放行）", uid, exc)
        return False
    finally:
        await client.aclose()

    if raw is None:
        return False
    try:
        revoked_after = float(raw)
    except (TypeError, ValueError):
        return False
    return float(iat) < revoked_after
