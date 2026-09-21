"""AuditMiddleware 自动记录 admin 写操作（CP3.6-A1）。v1 §3.6 5 原则 1。"""

import asyncio
import json
import re
from typing import Optional

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models.admin_operation_log import AdminOperationLog

_log = structlog.get_logger("audit")

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


# 全局 registry：每个 AuditMiddleware 实例 fire-and-forget 写的 task 都注册进来，
# lifespan shutdown 时 await 全部 task，避免 asyncio 强杀任务导致 admin log 漏写。
_pending_tasks: set[asyncio.Task] = set()


def _track_task(coro) -> asyncio.Task:
    """创建 task 并注册到全局 set；任务完成后自动从 set 移除。"""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


async def drain_pending_audit_tasks(timeout: float = 5.0) -> None:
    """等待残留 audit task 完成（lifespan shutdown 时调用）。

    返回前最多等 timeout 秒；超时后未完成的任务会被取消（task 取消是 asyncio 标准语义）。
    """
    if not _pending_tasks:
        return
    pending = list(_pending_tasks)
    _log.info("audit_draining", pending=len(pending), timeout=timeout)
    done, not_done = await asyncio.wait(pending, timeout=timeout)
    if not_done:
        _log.error(
            "audit_drain_timeout",
            pending=len(not_done),
            note="audit log 写入超时，已强制取消 — 可能有 admin 操作未记 log",
        )
        for t in not_done:
            t.cancel()
    else:
        _log.info("audit_drained", completed=len(done))


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
        response = await call_next(request)

        # 5. 异步写 log（不阻塞响应）；失败用 ERROR 级别（之前静默吞）
        try:
            admin_user = await _resolve_admin_user(request)
            if admin_user is None:
                return response
            action = _extract_action(request.url.path)
            target_type, target_id = _extract_target(request.url.path)
            reason = (body_dict or {}).get("reason", "(no reason)")
            sanitized = _sanitize_body(body_dict)

            _track_task(
                self._write_log(
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
                )
            )
        except Exception as e:
            _log.error(
                "audit_dispatch_failed",
                path=request.url.path,
                method=request.method,
                err=str(e),
                note="audit middleware 调度失败（非写 log 失败），但响应已发出",
            )

        return response

    @staticmethod
    async def _write_log(**kwargs):
        """独立 task 写 log（不阻塞主请求）。失败升级到 ERROR。"""
        try:
            async with AsyncSessionLocal() as db:
                log_row = AdminOperationLog(**kwargs)
                db.add(log_row)
                await db.commit()
        except Exception as e:
            _log.error(
                "audit_write_failed",
                err=str(e),
                kwargs_summary={
                    k: kwargs.get(k)
                    for k in ("action", "target_type", "target_id", "method", "path")
                },
                note="admin operation log 写入失败 — CP3.6-A1 合规缺口，需要排查 DB 连接 / 权限",
            )


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
