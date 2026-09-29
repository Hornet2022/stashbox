"""CP-LLM-TEST-ERR：/admin/llm/test 错误分类器单测。

只测 admin_router._classify_llm_error（纯函数）：把任意异常压成
{kind, status_code, hint, detail}，并保证：
  - httpx 家族异常按状态码 / 类型正确归类
  - 非 httpx 异常（KeyError / JSONDecodeError / ValueError）归 internal
  - 嵌套 RuntimeError（openai client retry 3 次失败）能解开 cause
  - 脱敏：Authorization / Bearer / api_key=xxx 不会原样泄露
  - 限长：超长 detail 截断到 500 + '…'，避免前端 / 日志爆

不需要 DB / Redis / FastAPI app，CI 跑得动。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

# 让 admin_router 能被 import：
#   1. 仓库根放进 sys.path（解析 stashbox.*）
#   2. content-service/ 放进 sys.path（admin_router 是顶层模块）
import os

_REPO_ROOT = Path(__file__).resolve().parents[3]  # /Users/hornet/work/stashbox
_CONTENT_SERVICE = _REPO_ROOT / "backend" / "content-service"
for p in (str(_REPO_ROOT), str(_CONTENT_SERVICE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# STASHBOX_ALLOW_DEV_JWT=1 让 config.Settings 通过校验（admin_router 的
# clients.ai_client 间接 import settings）。
os.environ.setdefault("STASHBOX_ALLOW_DEV_JWT", "1")

# 精确按文件路径加载 content-service/admin_router.py（避开 ai-service 同名模块冲突）
_admin_router_path = _CONTENT_SERVICE / "admin_router.py"
_spec = importlib.util.spec_from_file_location(
    "_test_admin_llm_classifier_router", _admin_router_path
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

LLM_TEST_ERROR_KINDS = _module.LLM_TEST_ERROR_KINDS
_LLM_ERROR_HINTS = _module._LLM_ERROR_HINTS
_classify_llm_error = _module._classify_llm_error


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http_error(
    status_code: int,
    *,
    body: dict | None = None,
    headers: dict | None = None,
) -> httpx.HTTPStatusError:
    """造一个 httpx.HTTPStatusError；body / headers 可选。"""
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(
        status_code,
        request=req,
        headers=headers or {},
        content=json.dumps(body or {}).encode(),
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=req,
        response=resp,
    )


# ---------------------------------------------------------------------------
# 全集覆盖：error_kind 是 LLM_TEST_ERROR_KINDS 子集
# ---------------------------------------------------------------------------


def test_known_kinds_is_a_frozenset_and_matches_hints():
    assert isinstance(LLM_TEST_ERROR_KINDS, frozenset)
    # 每个 kind 都必须有对应中文 hint（不然前端会拿到空引导）
    for k in LLM_TEST_ERROR_KINDS:
        assert k in _LLM_ERROR_HINTS, f"missing hint for kind={k}"
        assert _LLM_ERROR_HINTS[k], f"empty hint for kind={k}"


# ---------------------------------------------------------------------------
# httpx.HTTPStatusError：按状态码分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code, expected_kind",
    [
        (400, "badreq"),
        (401, "auth"),
        (403, "forbidden"),
        (404, "notfound"),
        (408, "badreq"),  # 不是 timeout（httpx 自己抛 TimeoutException 才算 timeout）
        (422, "badreq"),
        (429, "ratelimit"),
        (500, "internal"),
        (502, "internal"),
        (503, "internal"),
        (418, "badreq"),  # 未列出的 4xx 兜底 badreq
    ],
)
def test_http_status_error_classified_by_status(status_code: int, expected_kind: str):
    cls = _classify_llm_error(_http_error(status_code))
    assert cls["kind"] == expected_kind
    assert cls["status_code"] == status_code
    # 任何 kind 都应该有 hint（即使错配也不该空）
    assert cls["hint"], f"empty hint for kind={expected_kind}"


def test_ratelimit_includes_retry_after_header():
    cls = _classify_llm_error(_http_error(429, headers={"retry-after": "30"}))
    assert cls["kind"] == "ratelimit"
    assert "30" in cls["hint"]
    assert "Retry-After" in cls["hint"]


def test_ratelimit_without_retry_after_still_works():
    cls = _classify_llm_error(_http_error(429))
    assert cls["kind"] == "ratelimit"
    assert "Retry-After" not in cls["hint"]


# ---------------------------------------------------------------------------
# httpx 传输层异常
# ---------------------------------------------------------------------------


def test_timeout_exception_classified_as_timeout():
    cls = _classify_llm_error(httpx.ReadTimeout("read timeout"))
    assert cls["kind"] == "timeout"
    assert cls["status_code"] is None


def test_connect_timeout_classified_as_timeout():
    """ConnectTimeout 是 TimeoutException 子类，归 timeout 而非 connect。"""
    cls = _classify_llm_error(httpx.ConnectTimeout("connect timeout"))
    assert cls["kind"] == "timeout"


def test_connect_error_classified_as_connect():
    cls = _classify_llm_error(httpx.ConnectError("name resolution failed"))
    assert cls["kind"] == "connect"
    assert cls["status_code"] is None


def test_network_error_falls_back_to_network():
    """读超时被前面的 TimeoutException 截走；这里用一个非 Timeout 的网络错。"""
    cls = _classify_llm_error(httpx.RemoteProtocolError("server reset"))
    assert cls["kind"] == "network"


# ---------------------------------------------------------------------------
# 内部代码错误：不该把原始异常名吐给用户
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("choices"),
        ValueError("api_key required for OpenAIClient"),
        json.JSONDecodeError("Expecting value", "", 0),
    ],
)
def test_internal_errors_classified_as_internal(exc):
    cls = _classify_llm_error(exc)
    assert cls["kind"] == "internal"
    assert cls["status_code"] is None
    assert "联系开发" in cls["hint"] or "内部" in cls["hint"]


# ---------------------------------------------------------------------------
# 嵌套异常：openai client retry 3 次后抛 RuntimeError，cause 是真正的 httpx 异常
# ---------------------------------------------------------------------------


def test_nested_runtime_error_unwraps_cause():
    """模拟 openai._post 在 3 次 retry 后抛 RuntimeError("... after 3 attempts: ...")。

    分类器应能解开 __cause__ 找到真正的 httpx.HTTPStatusError，按其分类。
    """
    inner = _http_error(401)
    try:
        try:
            raise inner
        except httpx.HTTPStatusError as e:
            raise RuntimeError("openai chat failed after 3 attempts: 401") from e
    except RuntimeError as wrapped:
        cls = _classify_llm_error(wrapped)

    assert cls["kind"] == "auth"
    assert cls["status_code"] == 401


# ---------------------------------------------------------------------------
# 脱敏：Authorization / Bearer / api_key 不能原样漏出去
# ---------------------------------------------------------------------------


def test_redacts_authorization_header_in_detail():
    raw = (
        "HTTPStatusError: Client error '401 Unauthorized' for url "
        "'https://api.openai.com/v1/chat/completions'. "
        "Request headers: Authorization: Bearer sk-abc123def456. "
        "Response body: {'error': 'invalid api key'}"
    )

    # 直接造一个带这段 str() 输出的"假"异常类（绕过 httpx 真实构造）
    class _FakeExc(Exception):
        def __init__(self, msg: str) -> None:
            super().__init__(msg)

    # 把原始消息塞进 detail：通过 RunTime 抛 _FakeExc，强制走 inner.__cause__ 路径
    try:
        try:
            raise _FakeExc(raw)
        except _FakeExc as e:
            raise RuntimeError(raw) from e
    except RuntimeError as wrapped:
        cls = _classify_llm_error(wrapped)

    assert "sk-abc123def456" not in cls["detail"]
    assert "Bearer ***" in cls["detail"]


def test_redacts_api_key_query_param():
    raw = "openai chat failed: GET https://x.com/v1?api_key=sk-secret123"
    try:
        try:
            raise ValueError(raw)
        except ValueError as e:
            raise RuntimeError(raw) from e
    except RuntimeError as wrapped:
        cls = _classify_llm_error(wrapped)

    assert "sk-secret123" not in cls["detail"]
    assert "api_key=***" in cls["detail"]


# ---------------------------------------------------------------------------
# 限长：detail 超长截断 + 加省略号
# ---------------------------------------------------------------------------


def test_long_detail_is_truncated_with_ellipsis():
    huge = "x" * 2000
    cls = _classify_llm_error(ValueError(huge))
    assert len(cls["detail"]) <= 600  # 500 + '…\n' 之类，安全边距
    assert cls["detail"].endswith("…")
