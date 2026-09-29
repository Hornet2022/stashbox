"""CP-LLM-TEST-ERR：/admin/llm/test 端点集成测试。

不走真 LLM（避免污染 DB / Redis / 第三方供应商），而是用 monkeypatch 替换
admin_router 模块里的 reload，让它返回一个可控的 fake client：
  - fake client.chat() 可按测试需要抛指定异常 / 返回字符串

覆盖：
  - 成功路径：返回 ok=true + text，error_kind/status_code/hint/detail 全为 null
  - 401 路径：ok=false + error_kind='auth' + status_code=401
  - 404 路径：error_kind='notfound'
  - 429 路径：error_kind='ratelimit' + hint 里有 retry-after
  - timeout 路径：error_kind='timeout'
  - 内部异常：error_kind='internal'，不暴露原 KeyError 字样

所有测试都不碰 system_config 缓存，因为 reload 本身被替换掉了。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import importlib.util

import httpx
import pytest
from fastapi import FastAPI

# sys.path 准备 + admin_router 按文件路径加载（避开 ai-service 同名模块）
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTENT_SERVICE = _REPO_ROOT / "backend" / "content-service"
for p in (str(_REPO_ROOT), str(_CONTENT_SERVICE)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("STASHBOX_ALLOW_DEV_JWT", "1")

_admin_router_path = _CONTENT_SERVICE / "admin_router.py"
_spec = importlib.util.spec_from_file_location(
    "_test_admin_llm_endpoint_router", _admin_router_path
)
assert _spec is not None and _spec.loader is not None
admin_router = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = admin_router
_spec.loader.exec_module(admin_router)

from stashbox.backend.common.auth_admin import require_admin_or_operator  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """替身 LLMClient；chat() 按测试需要 raise / 返回。"""

    def __init__(self, *, raise_exc: BaseException | None = None, return_text: str | None = None):
        self._raise = raise_exc
        self._return = return_text
        self.calls: list[str] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def chat(self, prompt: str) -> str:  # noqa: D401
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        return self._return or "ok"

    async def close(self) -> None:
        return None


@pytest.fixture
async def admin_llm_env(monkeypatch):
    """组合 fixture：env + ASGI client + 注入 fake reload 的 helper。

    用法：
        async def test_xxx(admin_llm_env):
            client, set_reload = admin_llm_env
            set_reload(_FakeLLMClient(return_text="hi"))
            r = await client.get("/api/v1/admin/llm/test")
            ...
    """
    # 强制打开端点（兜底避免被老 disable 测试遗留的 env 影响）
    monkeypatch.delenv("ENABLE_LLM_TEST_ENDPOINT", raising=False)
    monkeypatch.setenv("ENABLE_LLM_TEST_ENDPOINT", "1")

    app = FastAPI()
    # 注册 BizException → JSON 响应处理器（与各 service main.py 同款），
    # 否则 NotFound（raise NotFound(...)) 会裸抛 500 而不是 404。
    from stashbox.backend.common.exceptions import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(admin_router.router)

    # 跳过 admin 鉴权依赖
    app.dependency_overrides[require_admin_or_operator] = lambda: {
        "sub": "999",
        "tier": "admin",
    }

    def _set_reload(fake_client: _FakeLLMClient) -> None:
        """把 admin_router.reload 替换成返回一个 fake client 的 async 函数。

        注意 reload 本身是 async（admin_router 里 `await reload()`），所以
        替换函数也必须是 async，否则 await 会 TypeError。
        """

        async def _reload() -> _FakeLLMClient:
            return fake_client

        monkeypatch.setattr(admin_router, "reload", _reload)

    ac = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    try:
        yield ac, _set_reload
    finally:
        await ac.aclose()


# ---------------------------------------------------------------------------
# helper：构造各种异常（与 classifier 测试同款）
# ---------------------------------------------------------------------------


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


async def test_endpoint_success_returns_text_and_null_error_fields(admin_llm_env):
    client, set_reload = admin_llm_env
    set_reload(_FakeLLMClient(return_text="这是 LLM 的回复"))
    r = await client.get("/api/v1/admin/llm/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "fake"
    assert body["model"] == "fake-model"
    assert body["text"] == "这是 LLM 的回复"
    # 成功时错误相关字段必须为 null，前端 schema 才能稳定
    assert body["error_kind"] is None
    assert body["status_code"] is None
    assert body["hint"] is None
    assert body["detail"] is None
    # 兼容字段 error 也必须为 null（不能保留旧版本的 hint 残值）
    assert body["error"] is None


# ---------------------------------------------------------------------------
# 失败路径：每个 error_kind 一个 case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_kind, expected_status",
    [
        (_http_error(400), "badreq", 400),
        (_http_error(401), "auth", 401),
        (_http_error(403), "forbidden", 403),
        (_http_error(404), "notfound", 404),
        (_http_error(429), "ratelimit", 429),
        (_http_error(500), "internal", 500),
        (httpx.ConnectTimeout("timeout"), "timeout", None),
        (httpx.ConnectError("dns"), "connect", None),
    ],
)
async def test_endpoint_classifies_http_errors(admin_llm_env, exc, expected_kind, expected_status):
    client, set_reload = admin_llm_env
    set_reload(_FakeLLMClient(raise_exc=exc))
    r = await client.get("/api/v1/admin/llm/test")
    assert r.status_code == 200  # 端点永远 200，错误在 body 里
    body = r.json()
    assert body["ok"] is False
    assert body["error_kind"] == expected_kind
    assert body["status_code"] == expected_status
    # hint 不能为空、且必须是中文引导
    assert body["hint"], f"empty hint for kind={expected_kind}"
    assert any(
        "\u4e00" <= c <= "\u9fff" for c in body["hint"]
    ), f"hint should be Chinese: {body['hint']!r}"
    # 兼容字段 error 等于 hint（老前端拿这个字段）
    assert body["error"] == body["hint"]
    # 失败时不返回 text，避免和 error 语义冲突
    assert "text" not in body or body.get("text") is None
    # detail 一定存在（哪怕是空 detail）
    assert "detail" in body


async def test_endpoint_ratelimit_includes_retry_after_in_hint(admin_llm_env):
    client, set_reload = admin_llm_env
    set_reload(_FakeLLMClient(raise_exc=_http_error(429, headers={"retry-after": "60"})))
    r = await client.get("/api/v1/admin/llm/test")
    body = r.json()
    assert body["error_kind"] == "ratelimit"
    assert "60" in body["hint"]


async def test_endpoint_internal_exception_does_not_leak_raw_exception_name(admin_llm_env):
    """KeyError / JSONDecode 等不该把原异常名吐到 hint（用户看不懂）。"""
    client, set_reload = admin_llm_env
    set_reload(_FakeLLMClient(raise_exc=KeyError("choices")))
    r = await client.get("/api/v1/admin/llm/test")
    body = r.json()
    assert body["error_kind"] == "internal"
    # hint 里不能出现 "KeyError"
    assert "KeyError" not in body["hint"]
    # 但 detail 里应该有（运维查问题时能看到）
    assert "KeyError" in body["detail"]


async def test_endpoint_disabled_returns_404(admin_llm_env, monkeypatch):
    """ENABLE_LLM_TEST_ENDPOINT=0 时端点应返回 404（与原逻辑一致）。"""
    client, _ = admin_llm_env
    # 这个 case 要把 env 强制改 0；monkeypatch 会自己还原（fixture 退出时）
    monkeypatch.setenv("ENABLE_LLM_TEST_ENDPOINT", "0")
    r = await client.get("/api/v1/admin/llm/test")
    assert r.status_code == 404
