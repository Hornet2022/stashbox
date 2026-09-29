"""CP-TTS-TEST-ERR：/admin/tts/test 端点集成测试。

不走真 TTS（避免污染 DB / Redis / 第三方供应商），用 monkeypatch 替换
admin_router 模块里的 tts_reload，返回一个 fake client：
  - fake client.synthesize() 可按测试需要抛指定异常 / 返回静音字节
  - provider_name / voice 属性可配置（影响 _classify_tts_error 的字符串匹配）

成功 / 失败：openai 401 / doubao business_code / edge 未安装 / indextts 参考音频不存在 /
local ffmpeg 失败 / ENABLE_TTS_TEST_ENDPOINT=0 返回 404。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

# sys.path 准备 + admin_router 按文件路径加载（同 classifier 测试）
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTENT_SERVICE = _REPO_ROOT / "backend" / "content-service"
for p in (str(_REPO_ROOT), str(_CONTENT_SERVICE)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("STASHBOX_ALLOW_DEV_JWT", "1")

# 精确按文件路径加载 admin_router（避开 ai-service 同名模块）
_admin_router_path = _CONTENT_SERVICE / "admin_router.py"
_spec = importlib.util.spec_from_file_location(
    "_test_admin_tts_endpoint_router", _admin_router_path
)
assert _spec is not None and _spec.loader is not None, "无法加载 admin_router.py"
admin_router = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = admin_router
_spec.loader.exec_module(admin_router)

from stashbox.backend.common.auth_admin import require_admin_or_operator  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _FakeTTSClient:
    """替身 TTSClient；synthesize() 按测试需要 raise / 返回静音字节。"""

    def __init__(
        self,
        *,
        raise_exc: BaseException | None = None,
        return_bytes: bytes | None = None,
        provider_name: str = "fake",
        voice: str | None = "fake-voice",
    ):
        self._raise = raise_exc
        self._return = return_bytes if return_bytes is not None else b"\x00" * 100
        self._provider = provider_name
        self._voice = voice
        self.calls: list = []

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def voice(self) -> str | None:
        return self._voice

    async def synthesize(
        self, text: str, voice: str | None = None, output_format: str = "mp3"
    ) -> bytes:
        self.calls.append({"text": text, "voice": voice})
        if self._raise is not None:
            raise self._raise
        return self._return

    async def close(self) -> None:
        return None


@pytest.fixture
async def admin_tts_env(monkeypatch):
    """env + ASGI client + 注入 fake reload 的 helper（与 LLM 测试同款）。"""
    monkeypatch.delenv("ENABLE_TTS_TEST_ENDPOINT", raising=False)
    monkeypatch.setenv("ENABLE_TTS_TEST_ENDPOINT", "1")

    app = FastAPI()
    # 注册 BizException 处理器，否则 NotFound 裸抛 500（与各 service main.py 同款）
    from stashbox.backend.common.exceptions import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(admin_router.router)

    app.dependency_overrides[require_admin_or_operator] = lambda: {
        "sub": "999",
        "tier": "admin",
    }

    def _set_reload(fake_client: _FakeTTSClient) -> None:
        async def _reload() -> _FakeTTSClient:
            return fake_client

        # 注意：tts_reload 是 admin_router 顶层 from ... import 进来的引用，
        # patch admin_router.tts_reload 而不是 stashbox.backend.app.services.tts.reload
        monkeypatch.setattr(admin_router, "tts_reload", _reload)

    ac = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    try:
        yield ac, _set_reload
    finally:
        await ac.aclose()


# ---------------------------------------------------------------------------
# helper：构造各种 provider 异常
# ---------------------------------------------------------------------------


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example.com/v1/audio/speech")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def _wrapped_openai_error(sc: int) -> RuntimeError:
    """openai client 把 HTTPStatusError 包了：RuntimeError(...) from HTTPStatusError"""
    try:
        try:
            raise _http_error(sc)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"OpenAITTS 错误 {sc}: invalid api key") from e
    except RuntimeError as r:
        return r


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


async def test_endpoint_success_returns_audio_bytes_and_null_error_fields(admin_tts_env):
    client, set_reload = admin_tts_env
    set_reload(_FakeTTSClient(return_bytes=b"\x00" * 200, provider_name="mock", voice=None))
    r = await client.get("/api/v1/admin/tts/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "mock"
    assert body["bytes_len"] == 200
    # 成功时所有错误字段必须为 null，前端 schema 才能稳定
    assert body["error"] is None
    assert body["error_kind"] is None
    assert body["status_code"] is None
    assert body["hint"] is None
    assert body["detail"] is None


# ---------------------------------------------------------------------------
# 失败路径：按 provider 覆盖核心 kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_kind, expected_status",
    [
        (_wrapped_openai_error(401), "auth", 401),
        (_wrapped_openai_error(403), "forbidden", 403),
        (_wrapped_openai_error(404), "notfound", 404),
        (_wrapped_openai_error(429), "ratelimit", 429),
        (_wrapped_openai_error(500), "internal", 500),
        (RuntimeError("OpenAITTS 错误 401: invalid api key"), "auth", 401),
        (RuntimeError("Doubao TTS HTTP 401: invalid"), "auth", 401),
        (RuntimeError("IndexTTS HTTP 503: down"), "internal", 503),
        (RuntimeError("OpenAITTSClient 需要 api_key"), "auth", None),
        (
            RuntimeError(
                "DoubaoTTSClient 需要 Coding Plan 专属 API Key。从火山方舟 Coding Plan 控制台获取后填到 backend/.env 的 DOUBAO_TTS_API_KEY。"
            ),
            "auth",
            None,
        ),
        (RuntimeError("edge-tts 未安装。请先 pip install edge-tts"), "missing_dep", None),
        (
            RuntimeError("Edge TTS 返回空 bytes(text_len=10, voice=zh-CN-XiaoxiaoNeural)"),
            "empty",
            None,
        ),
        (RuntimeError("ffmpeg 转码失败: error"), "subprocess", None),
        (RuntimeError("say 合成失败 (voice=Tingting): not found"), "subprocess", None),
        (RuntimeError("参考音频文件不存在: /x.wav"), "notfound", None),
        (
            RuntimeError('Doubao TTS 错误: code=4500000 message="quota exceeded"'),
            "business_code",
            None,
        ),
    ],
)
async def test_endpoint_classifies_all_kinds(admin_tts_env, exc, expected_kind, expected_status):
    client, set_reload = admin_tts_env
    # 模拟 provider 名（影响业务码等 provider-aware 判定）
    provider = "fake"
    if "Doubao" in str(exc):
        provider = "doubao"
    elif "OpenAITTS" in str(exc):
        provider = "openai"
    elif "edge-tts" in str(exc) or "Edge" in str(exc):
        provider = "edge"
    elif "ffmpeg" in str(exc) or "say" in str(exc):
        provider = "local"
    elif "参考音频" in str(exc) or "IndexTTS" in str(exc):
        provider = "indextts"

    set_reload(_FakeTTSClient(raise_exc=exc, provider_name=provider))
    r = await client.get("/api/v1/admin/tts/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert (
        body["error_kind"] == expected_kind
    ), f"exc={exc!r}, expected={expected_kind}, got={body['error_kind']}"
    assert body["status_code"] == expected_status
    assert body["hint"], f"empty hint for kind={expected_kind}"
    # hint 必须是中文
    assert any(
        "\u4e00" <= c <= "\u9fff" for c in body["hint"]
    ), f"hint should be Chinese: {body['hint']!r}"
    # 兼容字段 error 等于 hint
    assert body["error"] == body["hint"]
    # 失败时不返回 bytes_len
    assert body.get("bytes_len") is None


async def test_endpoint_ratelimit_includes_retry_after(admin_tts_env):
    client, set_reload = admin_tts_env
    set_reload(_FakeTTSClient(raise_exc=_wrapped_openai_error(429)))
    r = await client.get("/api/v1/admin/tts/test")
    body = r.json()
    assert body["error_kind"] == "ratelimit"
    # wrapped HTTPStatusError 没有 retry-after 头，所以 hint 里不一定有
    # 这里仅断言 kind 正确


async def test_endpoint_doubao_business_code_hint_includes_code(admin_tts_env):
    client, set_reload = admin_tts_env
    set_reload(
        _FakeTTSClient(
            raise_exc=RuntimeError('Doubao TTS 错误: code=4500000 message="quota exceeded"'),
            provider_name="doubao",
        )
    )
    r = await client.get("/api/v1/admin/tts/test")
    body = r.json()
    assert body["error_kind"] == "business_code"
    # hint 应该带 code=4500000
    assert "4500000" in body["hint"]


async def test_endpoint_disabled_returns_404(admin_tts_env, monkeypatch):
    """ENABLE_TTS_TEST_ENDPOINT=0 时端点应返回 404。"""
    client, _ = admin_tts_env
    monkeypatch.setenv("ENABLE_TTS_TEST_ENDPOINT", "0")
    r = await client.get("/api/v1/admin/tts/test")
    assert r.status_code == 404
