"""
JWT 鉴权 - 签发 + 解析 + FastAPI 依赖。

用法:
    from stashbox.backend.common.auth import require_user

    @router.get("/me")
    async def get_me(user = Depends(require_user)):
        return {"user_id": user["user_id"]}
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Header, HTTPException, status
from jose import JWTError, jwt

from stashbox.backend.common.config import settings
from stashbox.backend.common import revocation


def create_access_token(
    user_id: str, extra: dict | None = None, expire_minutes: int | None = None
) -> str:
    """签发 JWT

    expire_minutes 默认用 settings.jwt_expire_minutes（7d，微信登录等）。
    admin login 等场景可传 expire_minutes=60 签发短时 token，不改默认行为。
    """
    if expire_minutes is None:
        expire_minutes = settings.jwt_expire_minutes
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expire_minutes)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": now,
        # 毫秒级签发时刻。
        #
        # JWT 的 iat 只有**秒**级精度 —— python-jose 3.5.0 编码时按
        # `claims[time_claim].utctimetuple()` 截断（jose/jwt.py:55-58），
        # 微秒直接丢掉。而吊销判据要回答的是「这个 token 是在登出**之前**
        # 还是**之后**签发的」—— 同一秒内签发的两个 token，用 iat 分不开。
        #
        # 不带这个戳，就只能在「误杀新 token」和「漏杀旧 token」之间二选一：
        # 宽一点，登出后立刻重新登录会被自己的新 token 拒掉；严一点，
        # 登出同一秒签发的旧 token 还能继续用满 7 天。
        # 多带一个毫秒戳，判据就能精确到毫秒，两个错都不用犯。
        "iat_ms": int(now.timestamp() * 1000),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    """签发 refresh token：长有效期 + type=refresh 声明，便于刷新端点识别与区分。"""
    return create_access_token(
        user_id,
        extra={"type": "refresh"},
        expire_minutes=settings.jwt_refresh_expire_minutes,
    )


def decode_token(token: str) -> dict:
    """解析 JWT，失败抛 401"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_refresh_token(token: str) -> dict:
    """解析 refresh token：无效/过期 → 401；类型不符（非 type=refresh）→ 401。"""
    payload = decode_token(token)  # 无效/过期 → 401
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def decode_access_token(token: str) -> dict:
    """解析 access token：无效/过期 → 401；**是 refresh token → 401**。

    为什么必须显式把 refresh 挡在外面：

      - `create_access_token` 不打 type 声明，有效期 7 天；
      - `create_refresh_token` 打 `type=refresh`，有效期 30 天（access 的 4 倍多）；
      - 而业务端点用的 `require_user` 原来直接 `decode_token` —— 对两者一视同仁。

    结果是**一个本该只用于 `/auth/refresh-token` 的 token，能访问全部业务接口**。
    安卓端确实只把 access token 放进 Authorization 头（`AuthInterceptor`），
    所以这不是"客户端在用"的兼容问题，而是一道从来没关上的门。

    而且泄露后没有补救手段：refresh 端点做轮换（发新的 refresh）但**不吊销旧的**，
    攻击者拿一个泄露的 refresh 可以反复换出新的 30 天 —— 30 天根本不是上限。
    吊销要引入 jti + 黑名单状态，不在这次范围内，这里先把「能不能当 access 用」
    这件事关掉。

    注意 admin 端点不受影响也不需要担心越权：`require_admin` 的 `tier` 缺省是
    `free`，而 refresh token 不带 tier 声明 → 403，fail-closed。
    """
    payload = decode_token(token)
    if payload.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cannot be used as an access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """FastAPI 依赖：从 Authorization header 取 token，返回 user payload。

    走 `decode_access_token` 而不是 `decode_token`：refresh token 必须在业务
    端点被拒绝（理由见该函数的说明）。这是全项目业务鉴权的唯一入口
    （`require_admin` / `require_admin_or_operator` 都挂在它上面），所以
    「refresh 不能当 access 用」这一条只需要在这里守住一次。

    2026-10-21 加了**吊销检查**：注销账号后，该用户的旧 token 必须立刻作废
    （见 common/revocation.py）。判据是 token 的 iat 早于吊销时刻。
    这一步只多一次 Redis GET，不查库 —— 鉴权在热路径上，不能给每个请求
    加一次 DB 往返。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:]  # 去掉 "Bearer "
    payload = decode_access_token(token)
    if await revocation.is_revoked(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def require_user_optional(
    authorization: Annotated[str | None, Header()] = None,
) -> dict | None:
    """可选鉴权 - 用于匿名也能访问但登录有增强功能的接口。

    拿着 refresh token 来 = 视同未登录（返回 None），与「token 无效/过期」的
    现有行为一致 —— 都是不给权限、只是不报错。反过来说不会因为这次改动把
    原本能匿名访问的端点变成 401。

    已吊销的 token 同样按"未登录"处理（返回 None），而不是 401 ——
    对可选鉴权的端点来说，"没登录"比"报错了"更贴近语义。
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload = decode_access_token(authorization[7:])
    except HTTPException:
        return None
    if await revocation.is_revoked(payload):
        return None
    return payload
