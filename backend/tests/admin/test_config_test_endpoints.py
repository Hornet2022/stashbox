"""admin「测试调用」端点 —— 2026-10 新增（bug A5）。

背景：这个测试文件锁的是**测试调用必须验证请求里给的那份配置**，而不是库里
已保存的那份。

原实现 `testLlm()` / `testTts()` 不带任何参数打 GET，后端走 `reload()` /
`tts_reload()` 取的是**已保存**配置。后果是一条完整的静默失败链：

    运营改了 base_url → 点「测试调用」→ 看到的是**旧配置的绿灯**
      → 填错的地址通过测试 → 点保存 → 生产推理时才炸

整条链路上没有任何一处提示「测的不是你刚填的东西」，而这恰恰是那个页面
存在的唯一理由。故新增 POST 版本（携带表单态、不落库），GET 版保留兼容。

这里不测真实 LLM/TTS 服务（单测环境没有），而是断言**端点确实读了请求体**：
把 `create_adhoc_client` / `build_client` 替换成记录调用的桩，验证传进来的
provider / base_url / key 正是请求里的值 —— 而不是库里的值。
"""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


content_main = _load_app("_a5_test_content_main", "content-service/main.py")
content_app = content_main.app

# patch 要打在 admin_router 模块上（端点函数所在处），取法沿用同目录
# test_admin_misc.py 的既有做法：从 content_module 上拿 `admin_router_module`。
#
# 不能 `import admin_router`，也不能写死 `sys.modules["admin_router"]`：
# content-service 与 ai-service 各有一个同名 admin_router.py，main.py 用唯一
# 模块名按路径加载（见 content-service/main.py:139 的注释）。裸名查不到 →
# KeyError；查到别人的实例 → patch 打在没人用的对象上，静默失效。
_admin_router = content_main.admin_router_module


async def _make_admin() -> int:
    import uuid

    async with AsyncSessionLocal() as session:
        u = User(
            open_id="a5_test_" + uuid.uuid4().hex[:16],
            nickname="a5",
            tier="free",
            monthly_quota=5,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return int(u.id)


def _token(uid: int) -> str:
    return create_access_token(str(uid), extra={"tier": "admin"})


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# 桩：记录被构造出来的 client 参数，并返回可控的成功/失败
# ---------------------------------------------------------------------------
class _FakeLLMClient:
    # 注意签名：端点侧是 `build_client(config)` 位置传参，
    # 写成 `__init__(self, **kw)` 会 TypeError，然后被端点的 except 兜成
    # "响应格式非预期" —— 症状看着像桩没被调用，其实是桩自己炸了。
    def __init__(self, config=None, **kw):
        self.kw = config if config is not None else kw
        self.closed = False

    async def chat(self, *_a, **_k):
        return "ok"

    async def close(self):
        self.closed = True


class _FakeTTSClient:
    def __init__(self, config=None, **kw):
        self.kw = config if config is not None else kw
        self.closed = False

    async def synthesize(self, *_a, **_k):
        return b"\x00" * 32

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_llm_test_uses_request_body_not_saved_config(monkeypatch):
    """请求里的 provider/model/api_key/base_url 必须原样被用来造 client。"""
    uid = await _make_admin()
    created: dict = {}

    def _fake_create(config):
        created.update(config)
        return _FakeLLMClient(config)

    monkeypatch.setitem(_admin_router.build_adhoc_client.__globals__, "build_client", _fake_create)

    payload = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "sk-from-form",
        "base_url": "https://form-value.invalid/v1",
    }
    async with _client(_token(uid)) as c:
        r = await c.post("/api/v1/admin/llm/test", json=payload)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    # 断言用的是**表单里的**值 —— 这正是原 bug 缺的那一环
    assert created["provider"] == "openai"
    assert created["openai_llm_model"] == "gpt-4o-mini"
    assert created["openai_llm_api_key"] == "sk-from-form"
    assert created["openai_llm_base_url"] == "https://form-value.invalid/v1"
    assert body["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_llm_test_rejects_unknown_provider(monkeypatch):
    """非法 provider 直接 400，不要等建 client 才炸。"""
    uid = await _make_admin()
    async with _client(_token(uid)) as c:
        r = await c.post(
            "/api/v1/admin/llm/test",
            json={"provider": "nope", "model": "m", "api_key": "k"},
        )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == 40000


@pytest.mark.asyncio
async def test_llm_test_closes_its_client(monkeypatch):
    """一次性 client 用完必须关，否则每次点「测试调用」漏一个 httpx 连接池。"""
    uid = await _make_admin()
    holder: dict = {}

    def _fake_create(config):
        c = _FakeLLMClient(config)
        holder["c"] = c
        return c

    monkeypatch.setitem(_admin_router.build_adhoc_client.__globals__, "build_client", _fake_create)

    async with _client(_token(uid)) as c:
        r = await c.post(
            "/api/v1/admin/llm/test",
            json={"provider": "openai", "model": "m", "api_key": "k"},
        )
    assert r.status_code == 200, r.text
    assert holder["c"].closed is True, "一次性 client 没有被 close"


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tts_test_uses_request_body(monkeypatch):
    """TTS 侧同因：表单里的 base_url / key 必须真的进到 build_client。"""
    uid = await _make_admin()
    seen: dict = {}

    import stashbox.backend.app.services.tts as tts_pkg

    def _fake_build(config):
        seen.update(config)
        return _FakeTTSClient()

    monkeypatch.setattr(tts_pkg, "build_client", _fake_build, raising=False)

    payload = {
        "provider": "openai",
        "openai_api_key": "sk-form-tts",
        "openai_base_url": "https://tts-form.invalid/v1",
        "openai_model": "tts-1",
        "openai_voice": "alloy",
    }
    async with _client(_token(uid)) as c:
        r = await c.post("/api/v1/admin/tts/test", json=payload)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert seen.get("provider") == "openai"
    assert seen.get("openai_api_key") == "sk-form-tts"
    assert seen.get("openai_base_url") == "https://tts-form.invalid/v1"


@pytest.mark.asyncio
async def test_tts_test_does_not_persist(monkeypatch):
    """测试调用**绝不能**落库 —— 否则它就从一个只读操作变成了写操作。"""
    from stashbox.backend.common import system_config

    uid = await _make_admin()
    calls: list = []
    monkeypatch.setattr(
        system_config, "set_config", lambda *a, **k: calls.append((a, k)), raising=False
    )

    monkeypatch.setitem(_admin_router.__dict__, "build_client", lambda config: _FakeTTSClient())

    async with _client(_token(uid)) as c:
        r = await c.post(
            "/api/v1/admin/tts/test",
            json={"provider": "openai", "openai_base_url": "https://x.invalid/v1"},
        )

    assert r.status_code == 200, r.text
    assert calls == [], "测试调用写了 system_config —— 它不该有任何持久化副作用"


@pytest.mark.asyncio
async def test_llm_test_requires_admin():
    """未登录 401。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/v1/admin/llm/test", json={"provider": "openai", "model": "m", "api_key": "k"}
        )
    assert r.status_code == 401, r.text
