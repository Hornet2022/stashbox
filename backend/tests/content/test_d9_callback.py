"""
CP1.7 D9 入口单测：POST /api/v1/callback/d9-add-article（6 个 case）。

覆盖：匿名 device_id / 已登录扣配额 / 配额用尽 3001 / URL 不合法 2001 /
      无身份 4001 / ai-service 失败仍建文章。
"""
import httpx
import uuid

from stashbox.backend.common.auth import decode_token

from helpers import ai_client, article_row, client, content_main, new_user, quota_used

ANONYMOUS_USER_ID = content_main.ANONYMOUS_USER_ID
D9_URL = "/api/v1/callback/d9-add-article"


# ---------------------------------------------------------------------------
# 1. 匿名（device_id only）→ 建文章 + 不扣配额 + 回显 device_id
# ---------------------------------------------------------------------------
async def test_d9_anonymous_device_id_creates_article_without_quota(fake_ai_client):
    device_id = "device_" + uuid.uuid4().hex[:8]
    async with client(device_id=device_id) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_anon"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["device_id"] == device_id
    assert body["status"] == "distilling"  # ai-service 已接单
    assert body["task_id"].startswith("dst_")

    art = await article_row(body["article_id"])
    assert art is not None
    assert art.user_id == ANONYMOUS_USER_ID  # 匿名用 user_id=0 标记
    assert art.source == "wechat"
    assert await quota_used(ANONYMOUS_USER_ID) == 0  # 匿名不计费


# ---------------------------------------------------------------------------
# 2. 已登录（JWT）→ 扣 1 次配额 + 带 owner JWT 触发 ai-service
# ---------------------------------------------------------------------------
async def test_d9_logged_in_consumes_quota_once(fake_ai_client):
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_user"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["device_id"] is None

    assert await quota_used(uid) == 1  # D9 预扣 1 次

    # 触发 ai-service：带上 owner 的 JWT（ai-service 的 distill 端点有 require_user）
    assert len(fake_ai_client.calls) == 1
    call = fake_ai_client.calls[0]
    assert call["article_id"] == body["article_id"]
    assert int(decode_token(call["auth_token"])["sub"]) == uid


# ---------------------------------------------------------------------------
# 3. 配额用尽 → 3001
# ---------------------------------------------------------------------------
async def test_d9_quota_exhausted_returns_3001():
    _uid, token = await new_user(monthly_quota=1)

    async with client(token) as c:
        first = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_ok"})
        assert first.status_code == 200, first.text

        second = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_over"})

    assert second.status_code == 403, second.text
    assert second.json()["code"] == 3001  # v1 §3.0：月配额用尽


# ---------------------------------------------------------------------------
# 4. URL 不合法（非 http/https）→ 2001
# ---------------------------------------------------------------------------
async def test_d9_invalid_url_returns_2001(fake_ai_client):
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "ftp://example.com/a.mp3"})

    assert r.status_code == 400, r.text
    assert r.json()["code"] == 2001  # v1 §3.0：URL scheme 不支持
    assert fake_ai_client.calls == []  # 没建文章，也没触发蒸馏


# ---------------------------------------------------------------------------
# 5. 既无 JWT 也无 device_id → 4001
# ---------------------------------------------------------------------------
async def test_d9_missing_identity_returns_4001(fake_ai_client):
    async with client() as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_none"})

    assert r.status_code == 400, r.text
    assert r.json()["code"] == 4001
    assert fake_ai_client.calls == []


# ---------------------------------------------------------------------------
# 6. ai-service 调用失败 → D9 仍成功建文章，task_id=None
# ---------------------------------------------------------------------------
async def test_d9_ai_service_failure_still_creates_article(fake_ai_client):
    fake_ai_client.fail = True
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_ai_down"})

    assert r.status_code == 200, r.text  # 不抛 5xx —— D9 用户体验优先
    body = r.json()
    assert body["task_id"] is None
    assert body["status"] == "pending"

    art = await article_row(body["article_id"])
    assert art is not None and art.status == "pending"


# ---------------------------------------------------------------------------
# 7-9. ai_client 本身：成功 / ai-service 5xx / 网络错误（含 retry）—— 失败只 log 不抛
# ---------------------------------------------------------------------------
def _patch_http(monkeypatch, handler) -> list:
    """把 AIServiceClient 内部的 httpx.AsyncClient 换成 MockTransport，记录请求。"""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    class _MockClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ai_client.httpx, "AsyncClient", _MockClient)
    return seen


async def test_ai_client_trigger_distill_success(monkeypatch):
    seen = _patch_http(
        monkeypatch,
        lambda _req: httpx.Response(
            200, json={"article_id": "art_1", "task_id": "dst_1", "status": "started"}
        ),
    )

    result = await ai_client.AIServiceClient().trigger_distill("art_1", auth_token="jwt_xxx")

    assert result["task_id"] == "dst_1"
    assert seen[0].url.path == "/api/v1/articles/art_1/distill"
    assert seen[0].headers["Authorization"] == "Bearer jwt_xxx"


async def test_ai_client_returns_none_on_http_500(monkeypatch):
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(500, json={"detail": "boom"}))

    assert await ai_client.AIServiceClient().trigger_distill("art_1") is None
    assert len(seen) == 1  # 确定性失败，不重试


async def test_ai_client_returns_none_on_transport_error(monkeypatch):
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ai-service down")

    seen = _patch_http(monkeypatch, _boom)

    assert await ai_client.AIServiceClient().trigger_distill("art_1") is None
    assert len(seen) == 2  # max_retries=2
