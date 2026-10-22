"""登出与「我是谁」端点（2026-10-22 走查补齐）。

## 为什么补这两个端点

全工程功能闭环走查发现：客户端的 [AuthApi.logout] 一直在调
`POST /api/v1/auth/logout`，但**服务端从来没实现过** → 一直 404 →
客户端靠 `catch (_: Exception)` 兜住、照样清本地 token，
**服务端 token 却仍然有效** → 设备丢失、或用户以为登出了却没登出时，
会话在服务端还能继续用。

`GET /api/v1/auth/me` 同理：客户端模型（UserInfoResponse）定义齐全，
服务端没实现 → 404（它当时无人调用，是死接口）。

## 本文件锁住

A. 登出返回 {"logged_out": true}
B. **登出后同一个 token 立刻失效**（401）—— 这是这个端点存在的全部意义
C. 未登录调 /auth/me → 401
D. 登出不影响**别的用户**的 token
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
LOGIN_URL = "/api/v1/auth/wechat-login"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"
QUOTA_URL = "/api/v1/users/me/quota"


def _load_module(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def user_main():
    return _load_module("_cp_logout_user_main", "user-service/main.py")


@pytest.fixture
async def client(user_main):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_main.app), base_url="http://user-service"
    ) as c:
        yield c


async def _login(client, code: str | None = None) -> str:
    code = code or ("logout_" + uuid.uuid4().hex[:12])
    r = await client.post(LOGIN_URL, json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_logout_returns_logged_out(client):
    token = await _login(client)
    r = await client.post(LOGOUT_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["logged_out"] is True


@pytest.mark.asyncio
async def test_logout_makes_token_invalid_immediately(client):
    """登出后同一个 token 立刻失效 —— 这个端点存在的全部意义。"""
    token = await _login(client)
    # 登出前：能用
    r0 = await client.get(QUOTA_URL, headers={"Authorization": f"Bearer {token}"})
    assert r0.status_code == 200, r0.text

    r = await client.post(LOGOUT_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    # 登出后：立刻 401
    r1 = await client.get(QUOTA_URL, headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 401, f"登出后 token 仍然可用：{r1.text}"
    assert "revoked" in r1.text.lower()


@pytest.mark.asyncio
async def test_logout_does_not_affect_other_users(client):
    """吊销是**按用户**的 —— 不能把别人的会话一起废掉。"""
    token_a = await _login(client)
    token_b = await _login(client)

    r = await client.post(LOGOUT_URL, headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 200, r.text

    rb = await client.get(QUOTA_URL, headers={"Authorization": f"Bearer {token_b}"})
    assert rb.status_code == 200, f"别人的 token 被误伤：{rb.text}"


@pytest.mark.asyncio
async def test_me_returns_current_user(client):
    token = await _login(client)
    r = await client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"]
    assert "nickname" in body
    assert body["tier"]


@pytest.mark.asyncio
async def test_me_requires_login(client):
    r = await client.get(ME_URL)
    assert r.status_code == 401, r.text
