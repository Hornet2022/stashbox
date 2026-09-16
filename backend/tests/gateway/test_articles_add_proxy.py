"""
CP1.7.3 Bug 1 回归：POST /api/v1/articles/add 走 gateway（8100）。

和 test_wechat_login_proxy.py 同一个根因（functools.partial 被 fastapi 当成 body
模型），这条是 E2E/T3 实际踩到的那条路径：走 8100 加文章返回 422 而不是 article_id。

上游用 gateway/conftest.py 默认的真实 content-service（articles/add 本来就指向它），
验证身份、body、query 透传 + 4xx/5xx 原样回给客户端。
"""
import json
import uuid

from helpers import new_user

ARTICLES_ADD_URL = "/api/v1/articles/add"


def _body() -> dict:
    return {"url": f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex[:8]}", "source": "wechat"}


# ---------------------------------------------------------------------------
# 1. 带 JWT + JSON body → 到 content-service 并建出文章（修前在 gateway 就 422）
# ---------------------------------------------------------------------------
async def test_articles_add_json_body_reaches_content_service(gw, upstream_requests):
    _uid, token = await new_user()

    body = _body()
    r = await gw.post(
        ARTICLES_ADD_URL, json=body, headers={"Authorization": f"Bearer {token}"}
    )

    assert r.status_code == 200, r.text  # 修前 422：missing body.method
    assert r.json()["id"].startswith("art_")

    upstream = upstream_requests[-1]
    assert upstream.method == "POST"
    assert upstream.url.path == ARTICLES_ADD_URL
    assert json.loads(upstream.content) == body  # body 原样
    assert upstream.headers["Authorization"] == f"Bearer {token}"  # 身份原样带给下游


# ---------------------------------------------------------------------------
# 2. query params 透传
# ---------------------------------------------------------------------------
async def test_articles_add_query_params_passthrough(gw, upstream_requests):
    _uid, token = await new_user()

    r = await gw.post(
        ARTICLES_ADD_URL,
        json=_body(),
        params={"auto_distill": "1"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert upstream_requests[-1].url.params["auto_distill"] == "1"


# ---------------------------------------------------------------------------
# 3. 没带 JWT → content-service 401，gateway 原样透传（不在 gateway 侧吞掉）
# ---------------------------------------------------------------------------
async def test_articles_add_missing_auth_401_passthrough(gw):
    r = await gw.post(ARTICLES_ADD_URL, json=_body())

    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 4. 下游 4xx → 原样透传（body 非法 → content-service 422）
# ---------------------------------------------------------------------------
async def test_articles_add_4xx_passthrough(gw):
    _uid, token = await new_user()

    r = await gw.post(
        ARTICLES_ADD_URL,
        json={"source": "wechat"},  # 缺 url
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 422, r.text
    assert r.json()["code"] == 42200  # 下游 content-service 的校验错误


# ---------------------------------------------------------------------------
# 5. 下游 5xx → 原样透传，body 不改、且不重试
# ---------------------------------------------------------------------------
async def test_articles_add_5xx_passthrough_without_retry(gw, mount, stub_app, upstream_requests):
    mount(stub_app(500, {"detail": "content-service boom"}))

    r = await gw.post(ARTICLES_ADD_URL, json=_body(), headers={"device-id": "dev_x"})

    assert r.status_code == 500
    assert r.json() == {"detail": "content-service boom"}  # body 原样
    assert len(upstream_requests) == 1  # 确定性失败，不重试
