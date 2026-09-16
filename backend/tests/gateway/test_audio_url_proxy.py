"""
CP1.7.1 audio-url 代理单测：GET /api/v1/articles/{id}/audio-url（走 gateway 8100）。

覆盖：ready → 签名 URL（1h 过期）/ pending → 404 / 非 owner → 403 / 上游 path 正确。
"""
import uuid
from datetime import datetime, timezone

from helpers import new_article, new_task, new_user

OSS_AUDIO = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def audio_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}/audio-url"


async def _new_ready_article(uid: int) -> str:
    article_id = await new_article(uid, status="distilling")
    await new_task(
        article_id,
        status="done",
        audio_url=f"{OSS_AUDIO}/{article_id}.m4a",
        duration_sec=300,
    )
    return article_id


# ---------------------------------------------------------------------------
# 1. ready → 200 + mock 签名 URL（expires_at ≈ 现在 + 1h）
# ---------------------------------------------------------------------------
async def test_audio_url_ready_proxied_with_signed_url(gw, upstream_requests):
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    before = datetime.now(timezone.utc).timestamp()
    r = await gw.get(audio_url(article_id), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    assert body["audio_url"].startswith(f"{OSS_AUDIO}/{article_id}.m4a?")
    assert "OSSAccessKeyId=mock" in body["audio_url"]
    assert "Signature=mock" in body["audio_url"]
    assert body["duration_sec"] == 300

    expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
    assert before + 3600 - 5 <= expires_at <= before + 3600 + 5

    assert upstream_requests[-1].url.path == audio_url(article_id)


# ---------------------------------------------------------------------------
# 2. pending（还没音频）→ 404
# ---------------------------------------------------------------------------
async def test_audio_url_pending_404_passthrough(gw):
    uid, token = await new_user()
    article_id = await new_article(uid, status="pending")

    r = await gw.get(audio_url(article_id), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 404, r.text
    assert r.json()["code"] == 40400


# ---------------------------------------------------------------------------
# 3. 非 owner → 403
# ---------------------------------------------------------------------------
async def test_audio_url_not_owner_403_passthrough(gw):
    owner_uid, _owner_token = await new_user()
    _other_uid, other_token = await new_user()
    article_id = await _new_ready_article(owner_uid)

    r = await gw.get(
        audio_url(article_id), headers={"Authorization": f"Bearer {other_token}"}
    )

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 40300


# ---------------------------------------------------------------------------
# 4. 上游 5xx → gateway 透传，body 不改
# ---------------------------------------------------------------------------
async def test_audio_url_upstream_5xx_passthrough(gw, mount, stub_app):
    _uid, token = await new_user()
    mount(stub_app(502, {"detail": "content-service down"}))

    r = await gw.get(
        audio_url("art_" + uuid.uuid4().hex[:8]),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 502
    assert r.json() == {"detail": "content-service down"}
