"""
CP1.7 audio-url 端点单测：GET /api/v1/articles/{id}/audio-url（5 个 case）。

覆盖：ready 返回 mock OSS 签名 URL / pending 404 / distilling 404 / 非 owner 403 /
      过期时间 > 现在 + 1h。
"""
from datetime import datetime, timezone

from helpers import client, new_article, new_task, new_user

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
# 1. ready → 返回 mock OSS 签名 URL（带 Expires / Signature）
# ---------------------------------------------------------------------------
async def test_audio_url_ready_returns_signed_url():
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    async with client(token) as c:
        r = await c.get(audio_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    assert body["duration_sec"] == 300
    assert body["audio_url"].startswith(f"{OSS_AUDIO}/{article_id}.m4a?")
    assert "Expires=" in body["audio_url"]
    assert "OSSAccessKeyId=mock" in body["audio_url"]
    assert "Signature=mock" in body["audio_url"]


# ---------------------------------------------------------------------------
# 2. pending（还没有音频）→ 404
# ---------------------------------------------------------------------------
async def test_audio_url_pending_returns_404():
    uid, token = await new_user()
    article_id = await new_article(uid, status="pending")

    async with client(token) as c:
        r = await c.get(audio_url(article_id))

    assert r.status_code == 404, r.text
    assert r.json()["code"] == 40400


# ---------------------------------------------------------------------------
# 3. distilling（蒸馏中）→ 404
# ---------------------------------------------------------------------------
async def test_audio_url_distilling_returns_404():
    uid, token = await new_user()
    article_id = await new_article(uid, status="distilling")
    await new_task(article_id, status="running")

    async with client(token) as c:
        r = await c.get(audio_url(article_id))

    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 4. 非 owner → 403
# ---------------------------------------------------------------------------
async def test_audio_url_not_owner_returns_403():
    owner_uid, _owner_token = await new_user()
    _other_uid, other_token = await new_user()
    article_id = await _new_ready_article(owner_uid)

    async with client(other_token) as c:
        r = await c.get(audio_url(article_id))

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 40300


# ---------------------------------------------------------------------------
# 5. 过期时间 > 现在 + 1h
# ---------------------------------------------------------------------------
async def test_audio_url_expires_in_one_hour():
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    before = datetime.now(timezone.utc).timestamp()
    async with client(token) as c:
        r = await c.get(audio_url(article_id))
    assert r.status_code == 200, r.text

    expires_at = datetime.fromisoformat(r.json()["expires_at"]).timestamp()
    assert expires_at > before + 3600 - 5  # 容忍 5s 误差
    assert expires_at <= before + 3600 + 5
