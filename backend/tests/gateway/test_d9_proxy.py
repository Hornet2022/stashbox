"""
CP1.7.1 D9 代理单测：POST /api/v1/callback/d9-add-article（走 gateway 8100）。

覆盖：匿名 device_id / Authorization 透传 / 4001 / 3001 / 上游 5xx 透传不重试 /
      X-Request-ID 链路（客户端带的 + gateway 生成的）。

注意：content-service 的 D9 端点是 `device_id: Header()`，FastAPI 把下划线转成连字符，
所以真实头名是 `device-id`（不是任务包 §5 里写的 X-Device-Id）—— 按实现来，不改 content-service。
"""
import uuid

from helpers import new_user

D9_URL = "/api/v1/callback/d9-add-article"


def _body(url_suffix: str) -> dict:
    return {"url": f"https://mp.weixin.qq.com/s/{url_suffix}", "source": "wechat"}


# ---------------------------------------------------------------------------
# 1. 无 Authorization + device-id → 转发到 content-service 并建文章
# ---------------------------------------------------------------------------
async def test_d9_anonymous_device_id_reaches_content_service(gw, upstream_requests):
    device_id = "device_" + uuid.uuid4().hex[:8]

    r = await gw.post(D9_URL, json=_body("d9_gw_anon"), headers={"device-id": device_id})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["device_id"] == device_id
    assert body["status"] == "distilling"

    upstream = upstream_requests[-1]
    assert upstream.method == "POST"
    assert upstream.url.path == D9_URL
    assert upstream.headers["device-id"] == device_id
    assert "authorization" not in upstream.headers  # 没带就不注入


# ---------------------------------------------------------------------------
# 2. 有 Authorization → 原样透传给 content-service
# ---------------------------------------------------------------------------
async def test_d9_authorization_passthrough(gw, upstream_requests):
    _uid, token = await new_user()

    r = await gw.post(
        D9_URL, json=_body("d9_gw_auth"), headers={"Authorization": f"Bearer {token}"}
    )

    assert r.status_code == 200, r.text
    assert r.json()["device_id"] is None  # 已登录走 user_id，不回显 device
    assert upstream_requests[-1].headers["Authorization"] == f"Bearer {token}"


# ---------------------------------------------------------------------------
# 3. 既无 JWT 也无 device_id → content-service 4001，gateway 原样透传
# ---------------------------------------------------------------------------
async def test_d9_missing_identity_4001_passthrough(gw):
    r = await gw.post(D9_URL, json=_body("d9_gw_none"))

    assert r.status_code == 400, r.text
    assert r.json()["code"] == 4001


# ---------------------------------------------------------------------------
# 4. 配额用尽 → content-service 3001（HTTP 403），gateway 原样透传
# ---------------------------------------------------------------------------
async def test_d9_quota_exhausted_3001_passthrough(gw):
    _uid, token = await new_user(monthly_quota=1)
    headers = {"Authorization": f"Bearer {token}"}

    first = await gw.post(D9_URL, json=_body("d9_gw_ok"), headers=headers)
    assert first.status_code == 200, first.text

    second = await gw.post(D9_URL, json=_body("d9_gw_over"), headers=headers)

    assert second.status_code == 403, second.text
    assert second.json()["code"] == 3001


# ---------------------------------------------------------------------------
# 5. 上游 5xx → gateway 原样透传，body 不改、且不重试
# ---------------------------------------------------------------------------
async def test_d9_upstream_5xx_passthrough_without_retry(gw, mount, stub_app, upstream_requests):
    mount(stub_app(500, {"detail": "content-service boom"}))

    r = await gw.post(D9_URL, json=_body("d9_gw_5xx"), headers={"device-id": "dev_x"})

    assert r.status_code == 500
    assert r.json() == {"detail": "content-service boom"}  # body 原样
    assert len(upstream_requests) == 1  # 确定性失败，不重试


# ---------------------------------------------------------------------------
# 6. X-Request-ID 链路：客户端带的 / gateway 生成的，都要能到上游
# ---------------------------------------------------------------------------
async def test_d9_client_request_id_propagates_to_upstream(gw, upstream_requests):
    rid = "rid_" + uuid.uuid4().hex[:8]

    r = await gw.post(
        D9_URL,
        json=_body("d9_gw_rid"),
        headers={"device-id": "dev_rid", "X-Request-ID": rid},
    )

    assert r.headers["X-Request-ID"] == rid
    assert upstream_requests[-1].headers["X-Request-ID"] == rid


async def test_d9_generated_request_id_propagates_to_upstream(gw, upstream_requests):
    r = await gw.post(D9_URL, json=_body("d9_gw_rid2"), headers={"device-id": "dev_rid2"})

    rid = r.headers["X-Request-ID"]
    assert rid.startswith("req_")  # 客户端没带 → gateway 生成
    assert upstream_requests[-1].headers["X-Request-ID"] == rid
