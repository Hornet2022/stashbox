"""
CP4.7-E2E-BACKEND 集成测 conftest：提供 auth fixture，跨服务测试用。

- 通过 gateway (8100) 登录拿 token
- fixture scope="session" 减少 db 重置（CP2 集成测踩过的坑）
"""
import asyncio
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

GATEWAY_URL = "http://localhost:8100"


@pytest.fixture(scope="session")
def event_loop():
    """session-scoped event loop（跨函数复用，减少连接池重建）。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def auth_token() -> str:
    """通过 /api/v1/auth/wechat-login 拿 JWT token（整个 session 复用同一用户）。"""
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        resp = await client.post(
            "/api/v1/auth/wechat-login",
            json={"code": "cp47_e2e_test_user"},
        )
        assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "access_token" in data, f"unexpected login response: {data}"
        return data["access_token"]


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    """直接返回 Authorization header（每个 test 函数换一次 token 不值得）。"""
    return {"Authorization": f"Bearer {auth_token}"}
