"""
CP1.7.1 audio-url 代理单测：GET /api/v1/articles/{id}/audio-url（走 gateway 8100）。

覆盖：ready → 签名 URL（1h 过期）/ pending → 404 / 非 owner → 403 / 上游 path 正确。
"""

import uuid

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
# 1. ready → 200 + 永久静态直链（expires_at=None）
# ---------------------------------------------------------------------------
# 口径变更（commit 985e963 "audio-url 端点给永久静态 url 伪造签名与过期时间"
# 之后又收敛过一次）：audio_url 多数是 bucket 公共读的**永久直链**，对它拼
# `?OSSAccessKeyId=mock&Signature=mock` 是两件错事 ——
#   1. 无意义的签名参数（SeaweedFS 只是靠"忽略未知参数"才没报错）；
#   2. 回报假 expires_at，客户端以为会过期，每次恢复播放/切档都白跑一次本接口。
# 现在干净的静态直链原样返回、expires_at=None（= 不会过期）。
# 库里本来就带签名参数时才沿用其过期语义。
async def test_audio_url_ready_returns_static_url_as_is(gw, upstream_requests):
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    r = await gw.get(audio_url(article_id), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    # 原样返回，不拼任何签名参数
    assert body["audio_url"] == f"{OSS_AUDIO}/{article_id}.m4a"
    assert "Signature=" not in body["audio_url"]
    assert "OSSAccessKeyId=" not in body["audio_url"]
    # 静态直链不会过期 → 必须告诉客户端"别来刷新"
    assert body["expires_at"] is None
    assert body["duration_sec"] == 300

    assert upstream_requests[-1].url.path == audio_url(article_id)


async def test_audio_url_keeps_signed_url_semantics(gw):
    """库里存的 URL **本来带签名** → 沿用其过期语义（接真 OSS RAM 后的分支）。"""
    uid, token = await new_user()
    article_id = await new_article(uid, status="distilling")
    signed = f"{OSS_AUDIO}/{article_id}.m4a?Expires=99999999999&OSSAccessKeyId=ak&Signature=abc"
    await new_task(article_id, status="done", audio_url=signed, duration_sec=300)

    r = await gw.get(audio_url(article_id), headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    # 已签名 URL 原样返回，不二次加工
    assert body["audio_url"] == signed
    assert body["expires_at"] is not None


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

    r = await gw.get(audio_url(article_id), headers={"Authorization": f"Bearer {other_token}"})

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
