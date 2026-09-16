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

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt

from stashbox.backend.common.config import settings


def create_access_token(user_id: str, extra: dict | None = None) -> str:
    """签发 JWT"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


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


async def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """FastAPI 依赖：从 Authorization header 取 token，返回 user payload"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:]  # 去掉 "Bearer "
    return decode_token(token)


async def require_user_optional(
    authorization: Annotated[str | None, Header()] = None,
) -> dict | None:
    """可选鉴权 - 用于匿名也能访问但登录有增强功能的接口"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return decode_token(authorization[7:])
    except HTTPException:
        return None
