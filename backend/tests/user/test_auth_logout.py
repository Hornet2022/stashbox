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


async def _login_full(client, code: str | None = None) -> dict:
    """登录并返回完整响应（含 refresh_token）。"""
    code = code or ("logout_" + uuid.uuid4().hex[:12])
    r = await client.post(LOGIN_URL, json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


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


@pytest.mark.asyncio
async def test_logout_kills_refresh_path_too(client):
    """登出后，**旧 refresh token 不能换出新 token**。

    这是本文件最重要的一条。2026-10-22 实测（logout 端点刚补上时）：
    logout 写了吊销标记，access token 确实 401 了，但拿**同一个
    refresh token** 去换新 token 依然返回 200 —— 因为 refresh 端点
    只查 users.deleted_at，从不看吊销标记。

    结果是「登出」只杀了 access token 这一条腿，refresh 那条腿还活着：
    设备丢了、用户以为登出了，旧 refresh token 最长 30 天内能无限换出
    新 access token —— 等于给用户发了一张「已登出」的假凭证。
    """
    body = await _login_full(client)
    refresh_token = body["refresh_token"]
    access_token = body["access_token"]

    # 登出前：refresh 能换出新 token
    r0 = await client.post("/api/v1/auth/refresh-token", json={"refresh_token": refresh_token})
    assert r0.status_code == 200, r0.text

    r = await client.post(LOGOUT_URL, headers={"Authorization": f"Bearer {access_token}"})
    assert r.status_code == 200, r.text

    # 登出后：旧 refresh token 必须失效
    r1 = await client.post("/api/v1/auth/refresh-token", json={"refresh_token": refresh_token})
    assert (
        r1.status_code == 401
    ), f"登出后旧 refresh token 仍能换出新 token —— 会话没死透：{r1.text}"


@pytest.mark.asyncio
async def test_fresh_login_after_logout_works(client):
    """登出后**重新登录**要能正常用。

    吊销判据用的是毫秒戳（token 里的 iat_ms vs Redis 里的吊销边界）。
    如果判据粗到「同一秒内签发的 token 一律作废」，那么「登出后立刻重新
    登录」会拿到一个**立刻失效**的 token —— 表现为登录成功但什么功能都
    报 401，极难排查。

    这条锁住毫秒精度带来的好处：登出前的 token 死透了，登出后的新 token
    活得好好的。
    """
    first = await _login_full(client)
    r = await client.post(LOGOUT_URL, headers={"Authorization": f"Bearer {first['access_token']}"})
    assert r.status_code == 200, r.text

    second = await _login_full(client)
    r2 = await client.get(QUOTA_URL, headers={"Authorization": f"Bearer {second['access_token']}"})
    assert r2.status_code == 200, f"重新登录后的 token 不该失效：{r2.text}"
