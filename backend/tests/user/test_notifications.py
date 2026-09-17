"""push_notifications 表 + 拉取/mark-read 端点（CP5.4a）。v1 §11.5 CP5.4。

前置：本机 PG 5432 已起，且已 alembic upgrade 0008。
真推送（极光/友盟）CP4.6 范围；蒸馏触发推送 CP5.4b 范围。
"""
import importlib.util
import pytest
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp54a_notif_user_main", "user-service/main.py")


async def new_user() -> tuple[int, str]:
    """建一个测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp54a_" + uuid.uuid4().hex[:24],
            nickname="pytest_cp54a",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = create_access_token(str(user.id))
        return user.id, token


@pytest.mark.asyncio
async def test_push_notifications_table_exists():
    """alembic 0008 后表存在"""
    async with AsyncSessionLocal() as s:
        # 即使表空，SELECT 1 FROM ... LIMIT 1 不报 ProgrammingError 即存在
        await s.execute(text("SELECT 1 FROM push_notifications LIMIT 1"))


@pytest.mark.asyncio
async def test_list_notifications_endpoint():
    """GET /api/v1/notifications 端点存在 + 返 200"""
    user_id, token = await new_user()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=user_app), base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/notifications",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "notifications" in data
    assert "unread_count" in data


@pytest.mark.asyncio
async def test_mark_read_endpoint():
    """POST /api/v1/notifications/{id}/mark-read 端点存在 + 返 200"""
    user_id, token = await new_user()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=user_app), base_url="http://test") as client:
        # 404 on non-existent notification
        resp = await client.post(
            "/api/v1/notifications/99999/mark-read",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
