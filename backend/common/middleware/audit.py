"""AuditMiddleware 自动记录 admin 写操作（CP3.6-A1）。v1 §3.6 5 原则 1。"""
import json
import re
import time
from typing import Optional
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models.admin_operation_log import AdminOperationLog

# 路径匹配：/api/v1/admin/* 写操作
ADMIN_PATH_PATTERN = re.compile(r"^/api/v1/admin/")
WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# 敏感字段脱敏（request_body 写到 log 前过滤）
SENSITIVE_FIELDS = {"password", "token", "secret", "phone", "id_card"}


def _sanitize_body(body: Optional[dict]) -> Optional[dict]:
    """脱敏敏感字段"""
    if not body:
        return body
    sanitized = {}
    for k, v in body.items():
        if k.lower() in SENSITIVE_FIELDS:
            sanitized[k] = "***REDACTED***"
        else:
            sanitized[k] = v
    return sanitized


def _extract_action(path: str) -> str:
    """从路径提取 action 名（quota_adjust / force_retry / ...）"""
    # /api/v1/admin/users/123/quota-adjust → quota_adjust
    parts = path.rstrip("/").split("/")
    return parts[-1].replace("-", "_") if parts else "unknown"


def _extract_target(path: str) -> tuple[Optional[str], Optional[str]]:
    """从路径提取 (target_type, target_id)"""
    # /api/v1/admin/users/123/quota-adjust → ("user", "123")
    parts = path.split("/")
    try:
        idx = parts.index("admin")
        if idx + 2 < len(parts):
            return parts[idx + 1], parts[idx + 2]
    except ValueError:
        pass
    return None, None


class AuditMiddleware(BaseHTTPMiddleware):
    """admin 写操作自动记录（CP3.6-A1）。

    触发条件：
    1. 路径匹配 /api/v1/admin/*
    2. 方法是 POST/PUT/DELETE/PATCH
    3. Authorization header 有有效 JWT

    注意：JWT 解码失败不破主请求，middleware 静默放过。
    """

    async def dispatch(self, request: Request, call_next):
        # 1. 过滤路径
        if not ADMIN_PATH_PATTERN.match(request.url.path):
            return await call_next(request)

        # 2. 过滤方法
        if request.method not in WRITE_METHODS:
            return await call_next(request)

        # 3. 读 body（用于 log）
        body_bytes = await request.body()
        body_dict = None
        if body_bytes:
            try:
                body_dict = json.loads(body_bytes)
            except Exception:
                pass

        # 4. 执行实际请求
        start = time.time()
        response = await call_next(request)

        # 5. 异步写 log（不阻塞响应）
        try:
            admin_user = await _resolve_admin_user(request)
            if admin_user is None:
                return response
            action = _extract_action(request.url.path)
            target_type, target_id = _extract_target(request.url.path)
            reason = (body_dict or {}).get("reason", "(no reason)")
            sanitized = _sanitize_body(body_dict)

            # fire-and-forget 写 log
            import asyncio
            asyncio.create_task(self._write_log(
                admin_id=admin_user["id"],
                admin_tier=admin_user.get("tier", "unknown"),
                action=action,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                method=request.method,
                path=request.url.path,
                request_body=sanitized,
                response_status=response.status_code,
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            ))
        except Exception:
            pass  # log 失败不破主请求

        return response

    @staticmethod
    async def _write_log(**kwargs):
        """独立 task 写 log（不阻塞主请求）"""
        try:
            async with AsyncSessionLocal() as db:
                log = AdminOperationLog(**kwargs)
                db.add(log)
                await db.commit()
        except Exception:
            pass  # log 失败静默


async def _resolve_admin_user(request: Request) -> Optional[dict]:
    """从 Authorization header 解 JWT 拿 user dict。

    注意：JWT 解码失败静默返回 None，不破主请求。
    """
    from stashbox.backend.common.config import settings
    from jose import jwt, JWTError

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return {"id": int(payload["sub"]), "tier": payload.get("tier", "unknown")}
    except (JWTError, ValueError, KeyError):
        return None
