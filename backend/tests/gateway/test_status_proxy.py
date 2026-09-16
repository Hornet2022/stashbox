"""
CP1.7.1 status 代理单测：GET /api/v1/articles/{id}/status（走 gateway 8100）。

覆盖：ready 200 + audio_url / 非 owner 403 / 不存在 404 / 转发 path 正确 / body 不被改写。
"""
from helpers import client, new_article, new_task, new_user

OSS_AUDIO = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def status_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}/status"


async def _new_ready_article(uid: int) -> str:
    article_id = await new_article(uid, status="distilling")
    await new_task(
        article_id,
        status="done",
        audio_url=f"{OSS_AUDIO}/{article_id}.m4a",
        duration_sec=300,
        tags=["科技", "商业"],
        quality_score=8.5,
    )
    return article_id


# ---------------------------------------------------------------------------
# 1. ready → 200 + audio_url（与直连 content-service 结果一致）
# ---------------------------------------------------------------------------
async def test_status_ready_proxied_with_audio_url(gw, upstream_requests):
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    r = await gw.get(status_url(article_id), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    assert body["status"] == "ready"
    assert body["audio_url"] == f"{OSS_AUDIO}/{article_id}.m4a"
    assert body["audio_duration_sec"] == 300
    assert body["tags"] == ["科技", "商业"]

    upstream = upstream_requests[-1]
    assert upstream.method == "GET"
    assert upstream.url.path == status_url(article_id)
    assert upstream.headers["Authorization"] == f"Bearer {token}"


# ---------------------------------------------------------------------------
# 2. 非 owner → content-service 403，gateway 透传
# ---------------------------------------------------------------------------
async def test_status_not_owner_403_passthrough(gw):
    owner_uid, _owner_token = await new_user()
    _other_uid, other_token = await new_user()
    article_id = await new_article(owner_uid, status="pending")

    r = await gw.get(
        status_url(article_id), headers={"Authorization": f"Bearer {other_token}"}
    )

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 40300


# ---------------------------------------------------------------------------
# 3. 文章不存在 → 404
# ---------------------------------------------------------------------------
async def test_status_not_found_404_passthrough(gw):
    _uid, token = await new_user()

    r = await gw.get(status_url("art_gw_not_exist"), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 404, r.text
    assert r.json()["code"] == 40400


# ---------------------------------------------------------------------------
# 4. gateway 不改 body：走网关 == 直连 content-service
# ---------------------------------------------------------------------------
async def test_status_body_unchanged_by_gateway(gw):
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)
    headers = {"Authorization": f"Bearer {token}"}

    via_gateway = await gw.get(status_url(article_id), headers=headers)
    async with client(token) as direct:
        straight = await direct.get(status_url(article_id))

    assert via_gateway.status_code == straight.status_code == 200
    assert via_gateway.json() == straight.json()
