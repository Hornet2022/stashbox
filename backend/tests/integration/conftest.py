"""
CP4.7-E2E-BACKEND 集成测 conftest：提供 auth fixture，跨服务测试用。

- 通过 gateway (18100 隔离实例) 登录拿 token
- fixture scope="session" 减少 db 重置（CP2 集成测踩过的坑）

这套用例的性质：**需要 4 个服务真跑**（user / content / ai / gateway）。
没有服务时不是「代码坏了」，而是「没起服务」—— 这两件事必须分开报，
否则每次全量 pytest 都会在这里稳定红 6 条，久了就没人看了
（这正是 content 目录被 --ignore 掉的同一个机制）。
"""

import asyncio

import os
import socket
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio

# 默认指向隔离实例（18100），不再默认打生产 8100。理由同
# tests/e2e/conftest.py：这套用例会真的建用户/文章/派蒸馏任务。
GATEWAY_URL = os.getenv("STASHBOX_GATEWAY", "http://127.0.0.1:18100")

# 生产端口。⚠️ 必须是 int：`urlparse(...).port` 返回 int，写成字符串集合会让
#    `8100 in {"8100"}` 恒为 False —— 守卫看着在、实际从不触发，比没有更危险。
_PRODUCTION_PORTS = {8100, 8101, 8102, 8103, 8104}


def _assert_not_production(gateway: str) -> None:
    """守卫：这套 integration 会真的建用户/文章/派蒸馏任务，不能打到生产。"""
    port = urlparse(gateway).port
    if port not in _PRODUCTION_PORTS:
        return
    if os.getenv("STASHBOX_ALLOW_PROD_E2E") == "1":
        return

    # 用 **skip 而不是 raise**：这里跑在 conftest 导入期，raise 属于 collection
    # error，会让**整个 pytest 收集中断**（退出码 2）—— CI 里 1000+ 个与集成测
    # 毫无关系的用例一起跑不起来。tests/e2e/conftest.py 早已因此改成 skip，
    # 这里还是上一版的写法，属于同一个坑的两份拷贝。
    pytest.skip(
        f"跳过 integration 测试：STASHBOX_GATEWAY={gateway} 指向生产端口 {port}。\n"
        "  这套用例会真的建用户/文章/派蒸馏任务，打生产等于污染生产库 + 占 TTS 队列。\n"
        "  · 先起隔离后端再指过去（stashbox-admin-web/e2e-backend.sh，网关 :18100）\n"
        "  · 或显式确认后设 STASHBOX_ALLOW_PROD_E2E=1（不建议）",
        allow_module_level=True,
    )


_assert_not_production(GATEWAY_URL)


def _gateway_reachable(url: str, timeout: float = 1.5) -> bool:
    """网关端口能不能连上。

    只做 TCP connect，不发 HTTP 请求：这里要判断的是「服务起没起」，
    不是「接口返回什么」。用 HTTP 探活会被 404/401 之类干扰 ——
    网关活着但没配路由，也会得到一个响应，然后被误判成「服务在线」，
    于是真正的问题（路由缺失）又退化成后面那些看不懂的断言失败。
    """
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_gateway_up():
    """网关没起就整目录 skip，而不是让每条用例各自 ConnectError 一次。

    原来没有这层：`pytest tests/` 在没起服务的开发机上稳定产出 6 条
    `httpx.ConnectError: All connection attempts failed`，报错里没有任何
    「服务没起」的字样，看上去像网关路由坏了 —— 而实际只是没跑
    `e2e-backend.sh`。报错出现在别的用例名下、且随环境变，是最难排查的一类。

    探测只做一次（session 级 autouse）：connect 本身有 1.5s 超时，
    逐用例探测会让全量 pytest 白等十几秒。
    """
    if _gateway_reachable(GATEWAY_URL):
        return
    pytest.skip(
        f"跳过 integration 测试：网关 {GATEWAY_URL} 连不上（4 个服务没起）。\n"
        "  这些用例要 gateway/user/content/ai 全部真跑才有效。\n"
        "  起法：bash stashbox-admin-web/e2e-backend.sh start\n"
        "  然后确保 STASHBOX_GATEWAY 指向网关（默认 :18100）。"
    )


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
