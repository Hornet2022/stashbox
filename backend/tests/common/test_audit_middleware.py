"""AuditMiddleware admin 写操作自动记录（CP3.6-A1）。"""
import pytest
from sqlalchemy import text
from stashbox.backend.common.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_admin_operation_logs_table_exists():
    """alembic 0009 后表存在"""
    async with AsyncSessionLocal() as s:
        await s.execute(text("SELECT 1 FROM admin_operation_logs LIMIT 1"))


def test_middleware_path_filter():
    """AuditMiddleware 路径过滤：只匹配 /api/v1/admin/*"""
    from stashbox.backend.common.middleware.audit import ADMIN_PATH_PATTERN
    assert ADMIN_PATH_PATTERN.match("/api/v1/admin/users")
    assert ADMIN_PATH_PATTERN.match("/api/v1/admin/users/123/quota-adjust")
    assert not ADMIN_PATH_PATTERN.match("/api/v1/users/123")
    assert not ADMIN_PATH_PATTERN.match("/api/v1/tags")


def test_middleware_method_filter():
    """AuditMiddleware 方法过滤：只记录写操作"""
    from stashbox.backend.common.middleware.audit import WRITE_METHODS
    assert "POST" in WRITE_METHODS
    assert "PUT" in WRITE_METHODS
    assert "DELETE" in WRITE_METHODS
    assert "PATCH" in WRITE_METHODS
    assert "GET" not in WRITE_METHODS
    assert "HEAD" not in WRITE_METHODS
    assert "OPTIONS" not in WRITE_METHODS


def test_sanitize_body():
    """敏感字段脱敏"""
    from stashbox.backend.common.middleware.audit import _sanitize_body
    body = {"user_id": 123, "password": "abc", "phone": "13800000000", "token": "xyz"}
    sanitized = _sanitize_body(body)
    assert sanitized["user_id"] == 123
    assert sanitized["password"] == "***REDACTED***"
    assert sanitized["phone"] == "***REDACTED***"
    assert sanitized["token"] == "***REDACTED***"


def test_extract_action():
    """从路径提取 action"""
    from stashbox.backend.common.middleware.audit import _extract_action
    assert _extract_action("/api/v1/admin/users/123/quota-adjust") == "quota_adjust"
    assert _extract_action("/api/v1/admin/articles/456/force-retry") == "force_retry"
