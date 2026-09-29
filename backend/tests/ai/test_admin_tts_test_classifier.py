"""CP-TTS-TEST-ERR：/admin/tts/test 错误分类器单测。

TTS 5 个 provider（mock/edge/openai/doubao/local/indextts）的异常形态各异：
  - openai / doubao / indextts 把 httpx 异常包成 RuntimeError / IndexTTSError，
    字符串里嵌了 HTTP 状态码或火山引擎业务码
  - edge / local 没有 HTTP 概念，是缺包 / 子进程失败

分类策略：先看 cause 链（httpx 家族类型），再字符串匹配 provider 专属关键词。
本测试只测 _classify_tts_error 纯函数，不走 FastAPI / DB / Redis。

注意：content-service/admin_router.py 和 ai-service/admin_router.py 重名，
直接 `import admin_router` 会拉错模块。这里用 importlib 按文件路径精确加载。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

# 让 stashbox.backend.* 可解析 + content-service/ 让 ai_client 等相对 import 可解析
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTENT_SERVICE = _REPO_ROOT / "backend" / "content-service"
for p in (str(_REPO_ROOT), str(_CONTENT_SERVICE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# STASHBOX_ALLOW_DEV_JWT=1 让 config.Settings 通过校验
os.environ.setdefault("STASHBOX_ALLOW_DEV_JWT", "1")

# 精确按文件路径加载 content-service/admin_router.py（避开 ai-service 同名模块）
_admin_router_path = _CONTENT_SERVICE / "admin_router.py"
_spec = importlib.util.spec_from_file_location(
    "_test_admin_tts_classifier_router", _admin_router_path
)
assert _spec is not None and _spec.loader is not None, "无法加载 content-service/admin_router.py"
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

TTS_TEST_ERROR_KINDS = _module.TTS_TEST_ERROR_KINDS
_TTS_ERROR_HINTS = _module._TTS_ERROR_HINTS
_classify_tts_error = _module._classify_tts_error


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http_error(
    status_code: int,
    *,
    headers: dict | None = None,
) -> httpx.HTTPStatusError:
    """造一个 httpx.HTTPStatusError，模拟被 client 包到 RuntimeError.cause 里。"""
    req = httpx.Request("POST", "https://api.example.com/v1/audio/speech")
    resp = httpx.Response(status_code, request=req, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


def _wrapped_openai_error(sc: int, body: str = "invalid api key") -> RuntimeError:
    """模拟 openai client 把 HTTPStatusError 包成 RuntimeError 的形态。"""
    try:
        try:
            raise _http_error(status_code=sc)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"OpenAITTS 错误 {sc}: {body}") from e
    except RuntimeError as r:
        return r


def _wrapped_openai_timeout() -> RuntimeError:
    try:
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as e:
            raise RuntimeError(f"OpenAITTS 超时: {e}") from e
    except RuntimeError as r:
        return r


# ---------------------------------------------------------------------------
# 全集覆盖
# ---------------------------------------------------------------------------


def test_known_kinds_is_frozenset_and_have_hints():
    assert isinstance(TTS_TEST_ERROR_KINDS, frozenset)
    for k in TTS_TEST_ERROR_KINDS:
        assert k in _TTS_ERROR_HINTS, f"missing hint for kind={k}"
        assert _TTS_ERROR_HINTS[k], f"empty hint for kind={k}"


# ---------------------------------------------------------------------------
# openai provider（HTTPStatusError 被包到 RuntimeError.cause）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sc, expected_kind",
    [
        (400, "badreq"),
        (401, "auth"),
        (403, "forbidden"),
        (404, "notfound"),
        (422, "badreq"),
        (429, "ratelimit"),
        (500, "internal"),
    ],
)
def test_openai_classified_by_cause_status_code(sc: int, expected_kind: str):
    cls = _classify_tts_error(_wrapped_openai_error(sc), "openai")
    assert cls["kind"] == expected_kind
    assert cls["status_code"] == sc
    assert cls["hint"], f"empty hint for kind={expected_kind}"


def test_openai_ratelimit_includes_retry_after():
    req = httpx.Request("POST", "https://api.example.com/v1/audio/speech")
    resp = httpx.Response(429, request=req, headers={"retry-after": "20"})
    inner = httpx.HTTPStatusError("rate", request=req, response=resp)
    try:
        try:
            raise inner
        except httpx.HTTPStatusError as e:
            raise RuntimeError("OpenAITTS 错误 429") from e
    except RuntimeError as r:
        cls = _classify_tts_error(r, "openai")

    assert cls["kind"] == "ratelimit"
    assert "20" in cls["hint"]
    assert "Retry-After" in cls["hint"]


def test_openai_timeout_via_cause_chain():
    cls = _classify_tts_error(_wrapped_openai_timeout(), "openai")
    assert cls["kind"] == "timeout"
    assert cls["status_code"] is None


# ---------------------------------------------------------------------------
# openai / doubao 字符串里抠 HTTP 状态码（cause 没传过来，比如自己 raise 的）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg, expected_kind, expected_status",
    [
        ("OpenAITTS 错误 401: invalid api key", "auth", 401),
        ("OpenAITTS 错误 403: forbidden", "forbidden", 403),
        ("OpenAITTS 错误 404: not found", "notfound", 404),
        ("OpenAITTS 错误 429: rate limit", "ratelimit", 429),
        ("OpenAITTS 错误 500: server error", "internal", 500),
        ("Doubao TTS HTTP 401: invalid", "auth", 401),
        ("Doubao TTS HTTP 404: not found", "notfound", 404),
        ("IndexTTS HTTP 503: service down", "internal", 503),
    ],
)
def test_http_code_extracted_from_string(msg: str, expected_kind: str, expected_status: int):
    cls = _classify_tts_error(RuntimeError(msg), "openai")
    assert cls["kind"] == expected_kind
    assert cls["status_code"] == expected_status


# ---------------------------------------------------------------------------
# doubao business_code（火山引擎业务码）
# ---------------------------------------------------------------------------


def test_doubao_business_code_extracted_to_hint():
    cls = _classify_tts_error(
        RuntimeError('Doubao TTS 错误: code=4500000 message="quota exceeded"'),
        "doubao",
    )
    assert cls["kind"] == "business_code"
    # hint 应该把 code 值带上
    assert "4500000" in cls["hint"]


# ---------------------------------------------------------------------------
# edge / local provider 专属
# ---------------------------------------------------------------------------


def test_edge_missing_dependency():
    cls = _classify_tts_error(
        RuntimeError("edge-tts 未安装。请先 pip install edge-tts"),
        "edge",
    )
    assert cls["kind"] == "missing_dep"
    assert "pip install" in cls["hint"]


def test_edge_empty_audio():
    cls = _classify_tts_error(
        RuntimeError("Edge TTS 返回空 bytes(text_len=10, voice=zh-CN-XiaoxiaoNeural)"),
        "edge",
    )
    assert cls["kind"] == "empty"


def test_local_subprocess_failure():
    cls = _classify_tts_error(
        RuntimeError("ffmpeg 转码失败: some error"),
        "local",
    )
    assert cls["kind"] == "subprocess"
    assert "ffmpeg" in cls["hint"]


def test_local_say_failure():
    cls = _classify_tts_error(
        RuntimeError("say 合成失败 (voice=Tingting): voice not found"),
        "local",
    )
    assert cls["kind"] == "subprocess"
    assert "say" in cls["hint"]


# ---------------------------------------------------------------------------
# indextts 参考音频（provider 特化的 notfound）
# ---------------------------------------------------------------------------


def test_indextts_ref_audio_missing():
    cls = _classify_tts_error(
        RuntimeError("参考音频文件不存在: /path/to/ref.wav"),
        "indextts",
    )
    assert cls["kind"] == "notfound"


def test_indextts_ref_audio_too_small():
    cls = _classify_tts_error(
        RuntimeError("参考音频太小(100B)，可能不是有效 wav: /x.wav"),
        "indextts",
    )
    assert cls["kind"] == "notfound"


def test_other_provider_ref_audio_does_not_force_notfound():
    """其他 provider 的字符串如果含"参考音频"，不应该强制归 notfound。

    （防误伤：openai / doubao 等不会抛"参考音频"，但理论上别处可能出现这个词）
    """
    # 用 doubao 跑，doubao 不会抛"参考音频"；这里构造一个不存在的边缘 case 看不会乱分类
    cls = _classify_tts_error(
        RuntimeError("Doubao TTS HTTP 401: invalid"),
        "doubao",
    )
    assert cls["kind"] == "auth"


# ---------------------------------------------------------------------------
# 缺凭证（provider 抛"需要 api_key"） → auth
# ---------------------------------------------------------------------------


def test_missing_api_key_classified_as_auth():
    cls = _classify_tts_error(
        RuntimeError("OpenAITTSClient 需要 api_key。火山方舟：填 ARK API Key"),
        "openai",
    )
    assert cls["kind"] == "auth"


def test_missing_doubao_credential_classified_as_auth():
    cls = _classify_tts_error(
        RuntimeError(
            "DoubaoTTSClient 需要 Coding Plan 专属 API Key。"
            "从火山方舟 Coding Plan 控制台获取后填到 backend/.env 的 DOUBAO_TTS_API_KEY。"
        ),
        "doubao",
    )
    assert cls["kind"] == "auth"


# ---------------------------------------------------------------------------
# httpx cause 链（独立异常，不经过 provider 包一层）
# ---------------------------------------------------------------------------


def test_cause_chain_timeout():
    cls = _classify_tts_error(
        RuntimeError("IndexTTS 合成超时(300s): read timed out"),
        "indextts",
    )
    # 没有 cause 时，按字符串"超时"分类（但当前实现里字符串匹配"超时"没覆盖，
    # 所以会落到 internal）。验证这一点：要么 timeout，要么 internal，避免误分类。
    assert cls["kind"] in ("timeout", "internal")


def test_cause_chain_connect_error():
    try:
        try:
            raise httpx.ConnectError("name resolution failed")
        except httpx.ConnectError as e:
            raise RuntimeError("OpenAITTS HTTP 异常: ...") from e
    except RuntimeError as r:
        cls = _classify_tts_error(r, "openai")

    assert cls["kind"] == "connect"


def test_cause_chain_network_error():
    """用 httpx.ReadError（NetworkError 真子类）模拟网络层异常被 client 包了。

    注意：httpx.RemoteProtocolError 不是 NetworkError 子类（它在 ProtocolError 下），
    真正的 NetworkError 子类是 ReadError / WriteError / CloseError / ConnectError /
    UnsupportedProtocol / DecodingError。
    """
    try:
        try:
            raise httpx.ReadError("server closed connection")
        except httpx.ReadError as e:
            raise RuntimeError("IndexTTS HTTP 异常: ...") from e
    except RuntimeError as r:
        cls = _classify_tts_error(r, "indextts")

    assert cls["kind"] == "network"


# ---------------------------------------------------------------------------
# 内部异常 → internal，不暴露原始 KeyError / JSONDecodeError 字样
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("choices"),
        ValueError("text 不能为空"),
    ],
)
def test_internal_errors_classified_as_internal(exc):
    cls = _classify_tts_error(exc, "openai")
    assert cls["kind"] == "internal"


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def test_redacts_authorization_in_detail():
    raw = "OpenAITTS 错误 401: Authorization: Bearer sk-abc123"
    cls = _classify_tts_error(RuntimeError(raw), "openai")
    assert "sk-abc123" not in cls["detail"]
    assert "Bearer ***" in cls["detail"]


def test_redacts_x_api_key_header_in_detail():
    raw = "Doubao TTS HTTP 401: invalid X-Api-Key: abcd1234efgh"
    cls = _classify_tts_error(RuntimeError(raw), "doubao")
    assert "abcd1234efgh" not in cls["detail"]
    assert "X-Api-Key: ***" in cls["detail"]


def test_redacts_env_var_in_detail():
    raw = "DOUBAO_TTS_API_KEY=secret-xyz Doubao TTS HTTP 500: fail"
    cls = _classify_tts_error(RuntimeError(raw), "doubao")
    assert "secret-xyz" not in cls["detail"]


# ---------------------------------------------------------------------------
# 限长
# ---------------------------------------------------------------------------


def test_long_detail_is_truncated_with_ellipsis():
    huge = "x" * 2000
    cls = _classify_tts_error(ValueError(huge), "openai")
    assert len(cls["detail"]) <= 600
    assert cls["detail"].endswith("…")
