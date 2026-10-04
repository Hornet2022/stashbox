"""网关 fallback 路由未命中时的 404 响应形状。

## 这条为什么重要

网关是安卓/后台进入服务的唯一入口。路由表没命中、且前缀 fallback 也不认时，
会走到 `proxy_fallback` 的 404 分支。修复前那里返回的是**裸 Response**：

    return Response(status_code=404, content=b'{"detail":"no downstream route"}')

裸 Response 绕过了 `register_exception_handlers` 装的所有处理器，响应体是
`{"detail": ...}` 而不是业务信封 `{code, message, data}`。

后果不是"多一个不认识的字段"，而是**安卓全部错误码分支同时失效**：
`ApiEnvelope.parseEnvelope()` 解不出信封 → `bizCode = 0` → 于是
`isAudioNotReady(40400)` / `isQuotaExceeded(3001)` / `isAuthExpired(40100)` /
`isCaptureFetchFailed(2001|2002)` 四个分支**全部落空**，最后落到 404 的
通用兜底文案「内容不存在或已删除」。

也就是说：**端点没上线**被误报成**文章被删了**。这比直接 500 更坏，
因为它把部署问题伪装成了用户数据问题，排障方向从一开始就是错的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path("/Users/hornet/work/stashbox/backend/api-gateway")
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import main as gw  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(gw.app)


#: 这些路径既不在 ROUTES 显式注册表里，第一段也不在 fallback 白名单
#: （白名单只有 articles / tags / callback / distill / user / subscription），
#: 所以必然走 404 分支。
#:
#: ⚠️ 挑这些路径时踩过一次坑：本来把 `/api/v1/notifications` 也列进来，
#: 但 `config.py:301` 明明注册了它（转发 user-service）—— 于是测试直接报红，
#: 反而纠正了"它没路由"的错误假设。**列进本清单前必须先确认真的没注册**，
#: 否则你会把一个正常接口当成 bug 去"修"。
UNMAPPED_PATHS = [
    "/api/v1/does_not_exist/xx",
    "/api/v1/admin/whatever",
    "/api/v1/favorites/999",
    "/api/v1/voice/whatever",
]


@pytest.mark.parametrize("path", UNMAPPED_PATHS)
def test_unmapped_route_returns_business_envelope(client, path):
    """未命中路由必须返回 {code, message, data}，而不是 {"detail": ...}。"""
    r = client.get(path)
    assert r.status_code == 404, r.text

    body = json.loads(r.text)
    for field in ("code", "message", "data"):
        assert field in body, f"响应缺信封字段 {field}: {body}"

    # 必须是 40400（与 common/exceptions._STATUS_CODE_MAP 的既有约定一致），
    # 不能是 404 本体 —— 安卓按 bizCode 分流，404 不是合法的业务码
    assert body["code"] == 40400, body
    assert "detail" not in body, f"退回 FastAPI 默认形状: {body}"


def test_envelope_message_is_not_empty(client):
    """message 不能空 —— 空文案在客户端会显示成空白错误提示。"""
    body = json.loads(client.get("/api/v1/does_not_exist/xx").text)
    assert isinstance(body["message"], str) and body["message"].strip()


def test_client_can_route_on_bizcode(client):
    """端到端意图：客户端读到 40400 就能区分「接口没上线」和「文章被删了」。

    这正是修复的目的 —— 原来 bizCode=0 让所有分支落空。
    """
    body = json.loads(client.get("/api/v1/does_not_exist/xx").text)
    biz_code = body["code"]
    # 安卓 ErrorMessages.kt 的分流条件
    assert biz_code != 0, "bizCode 不能是 0，否则客户端所有分支都落空"
    assert biz_code == 40400
