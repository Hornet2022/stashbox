"""
CP1.7.3 Bug 1 回归：POST /api/v1/auth/wechat-login 走 gateway（8100）。

CP1.7.1 用 `functools.partial(proxy, route=_route)` 注册路由表 —— fastapi 0.141.1
不再解 partial 的签名，把 Route 的字段（method / path / target_service…）当成
request body 模型去校验，带 JSON body 的 POST 一律 422：

    {"code":42200,"data":{"errors":[{"loc":["body","method"],"msg":"Field required"}]}}

改成闭包工厂 make_proxy(route) 后，注册的是普通 async 函数（签名只剩 request），
body / query / header 正常透传。

本文件把真实 user-service 挂成上游，逐项验证 §1.4 的 5 条。
"""
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

from stashbox.backend.common.auth import create_access_token

BACKEND_DIR = Path(__file__).resolve().parents[2]

WECHAT_LOGIN_URL = "/api/v1/auth/wechat-login"


@pytest.fixture(scope="session")
def user_app():
    """真实 user-service app 当上游（路由表里 wechat-login 指向 user-service）。"""
    spec = importlib.util.spec_from_file_location(
        "_cp173_wechat_user_main", BACKEND_DIR / "user-service" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_cp173_wechat_user_main"] = module
    spec.loader.exec_module(module)
    return module.app


@pytest.fixture(autouse=True)
def upstream(mount, user_app):
    """覆盖 gateway/conftest.py 的默认上游（content-service）→ user-service。"""
    return mount(user_app)


# ---------------------------------------------------------------------------
# 1. JSON body 透传：下游收到的就是客户端发的那份（修前在 gateway 就被 422 吃掉）
# ---------------------------------------------------------------------------
async def test_wechat_login_json_body_reaches_user_service(gw, upstream_requests):
    code = "cp173_" + uuid.uuid4().hex[:8]

    r = await gw.post(WECHAT_LOGIN_URL, json={"code": code})

    assert r.status_code == 200, r.text  # 修前 422：missing body.method
    body = r.json()
    assert body["user_id"]
    assert body["access_token"]

    upstream = upstream_requests[-1]
    assert upstream.method == "POST"
    assert upstream.url.path == WECHAT_LOGIN_URL
    assert json.loads(upstream.content) == {"code": code}  # body 原样


# ---------------------------------------------------------------------------
# 2. query params 透传
# ---------------------------------------------------------------------------
async def test_wechat_login_query_params_passthrough(gw, upstream_requests):
    r = await gw.post(
        WECHAT_LOGIN_URL,
        json={"code": "cp173_query"},
        params={"from": "miniapp", "invite": "42"},
    )

    assert r.status_code == 200, r.text
    params = upstream_requests[-1].url.params
    assert params["from"] == "miniapp"
    assert params["invite"] == "42"


# ---------------------------------------------------------------------------
# 3. Authorization 透传：gateway 不改写、不注入
# ---------------------------------------------------------------------------
async def test_wechat_login_authorization_passthrough(gw, upstream_requests):
    token = create_access_token("1")

    r = await gw.post(
        WECHAT_LOGIN_URL,
        json={"code": "cp173_auth"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert upstream_requests[-1].headers["Authorization"] == f"Bearer {token}"


# ---------------------------------------------------------------------------
# 4. 下游 4xx → 原样透传（body 非法 → user-service 422）
# ---------------------------------------------------------------------------
async def test_wechat_login_4xx_passthrough(gw):
    r = await gw.post(
        WECHAT_LOGIN_URL,
        content=b"not-a-json",
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 422, r.text
    assert r.json()["code"] == 42200  # 下游 user-service 的校验错误


# ---------------------------------------------------------------------------
# 5. 下游 5xx → 原样透传，body 不改、且不重试
# ---------------------------------------------------------------------------
async def test_wechat_login_5xx_passthrough_without_retry(gw, mount, stub_app, upstream_requests):
    mount(stub_app(500, {"detail": "user-service boom"}))

    r = await gw.post(WECHAT_LOGIN_URL, json={"code": "cp173_5xx"})

    assert r.status_code == 500
    assert r.json() == {"detail": "user-service boom"}  # body 原样
    assert len(upstream_requests) == 1  # 确定性失败，不重试
