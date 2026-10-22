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
- 签发于 T 之前的 token → 吊销前签发的 → 作废
- 签发于 T 之后的 token → 吊销后签发的 → 有效

用签发时间判定而不是"记一个当前 epoch 计数器"，是因为后者需要改所有签发点
（``create_access_token`` 有多个调用方），而签发时间本来就在 token 里。

## 精度：毫秒（不是秒）

JWT 的 ``iat`` 只有**秒**级精度 —— python-jose 3.5.0 编码时按
``claims[time_claim].utctimetuple()`` 截断（jose/jwt.py:55-58），微秒直接丢掉。
而吊销时刻是带小数秒的。拿秒级的 ``iat`` 去比一个带小数的边界，
**同一秒内签发的 token 就分不出"在吊销前还是吊销后"**。

两种做法都有一边是错的：
- 严（``iat <= 边界所在秒`` 视为已吊销）→ 登出后**同一秒内**重新登录，
  新签的 token 会被自己的吊销标记拒掉；
- 宽（``iat < 边界所在秒``）→ 登出同一秒签发的**旧** token 还能用满 7 天，
  "登出"就成了假动作。

所以 token 里额外带一个毫秒戳 ``iat_ms``（见 ``common/auth.py``），
吊销边界也按毫秒存 —— 判据精确到毫秒，两个错都不用犯。

旧 token（没有 ``iat_ms`` 的）退回秒级比较，判定从宽
（``iat <= 边界所在秒`` 视为已吊销），即**宁杀勿放**。

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

    记的是"吊销时刻"，判据是 token 的签发时刻早于它 → 作废。
    TTL 取 access token 的有效期：超过这个时长所有旧 token 自然过期，
    标记就没有存在意义了。

    ``at`` 是**秒**级时间戳（跟 ``datetime.timestamp()`` 同口径），
    存进 Redis 时换算成**毫秒** —— 判据要精确到毫秒，理由见模块说明。
    """
    ts = at if at is not None else time.time()
    ttl = max(60, settings.jwt_expire_minutes * 60)
    client = _client()
    try:
        # 带 "ms:" 前缀 —— 值自己说明自己是什么刻度。
        #
        # 原来靠"数值大小"猜刻度（小于 1e11 当秒），那是个**脆弱的启发式**：
        # 一旦有人拿一个 1970 时刻或未来很远的时刻做测试/回灌，
        # 刻度就会被判错，边界差 1000 倍 → 判据整个失效。
        # 前缀没有这种歧义。
        await client.set(_key(user_id), f"ms:{int(round(ts * 1000))}", ex=ttl)
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


def _parse_boundary_ms(raw: str | bytes) -> int | None:
    """把 Redis 里存的吊销边界读成**毫秒**。

    两种刻度：
    - 新写入的是 ``"ms:<整数>"`` —— **值自己说明刻度**，没有歧义；
    - 旧版本写入的是**裸的秒**（float，如 "1792680000.5"）—— 滚动发布
      期间 Redis 里可能还留着，按"小于 1e11 当秒"这个启发式认。

    1e11 的分界：一秒刻度要到公元 5138 年才越过，毫秒刻度在 1973 年
    就已经越过它 —— 现实中不会把这两个刻度搞混。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip()
    if text.startswith("ms:"):
        try:
            return int(text[3:])
        except ValueError:
            return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 1e11:  # 秒刻度（旧版本写的）
        return int(round(value * 1000))
    return int(round(value))  # 毫秒刻度（无前缀的裸毫秒值）


async def is_revoked(payload: dict[str, Any]) -> bool:
    """这个 token 是否已被吊销。

    - 查不到标记 → 没被吊销（正常路径，一次 Redis GET）
    - 标记存在且**签发时刻早于**吊销时刻 → 已吊销
    - Redis 不可用 → **放行**（fail-open，见模块说明）

    比较用 token 里的 ``iat_ms``（毫秒戳）。老 token 没有这个字段 →
    退回秒级 ``iat``，此时判定从宽：``iat <= 边界所在秒`` 就算已吊销
    （宁杀勿放 —— 分不清先后时，不能放过可能是旧的那一个）。
    """
    sub = payload.get("sub")
    if sub is None:
        return False
    iat_ms = payload.get("iat_ms")
    iat = payload.get("iat")
    if iat_ms is None and iat is None:
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
    boundary_ms = _parse_boundary_ms(raw)
    if boundary_ms is None:
        return False

    if iat_ms is not None:
        try:
            issued_ms = int(iat_ms)
        except (TypeError, ValueError):
            return False
        # 毫秒精度：签发时刻严格早于吊销时刻才算已吊销。
        # 同一毫秒内签发的新 token 不受影响 —— 登出后立刻重登不会被自己的
        # 吊销标记拒掉，而登出前签发的旧 token 也确实死透了。
        return issued_ms < boundary_ms

    # 老 token：只有秒级 iat，分不清同一秒内的先后 → 宁杀勿放。
    try:
        issued_sec = int(iat)
    except (TypeError, ValueError):
        return False
    return issued_sec <= boundary_ms // 1000
