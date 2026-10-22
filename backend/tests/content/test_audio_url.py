"""
CP1.7 audio-url 端点单测：GET /api/v1/articles/{id}/audio-url。

覆盖：ready 静态直链原样返回 / 预签名直链保留过期语义 / pending 404 /
      distilling 404 / 非 owner 403。

CP-AUDIO-URL-STATIC（2026-10 修用例）：本文件原先断言「ready 会拿到带
`?Expires=&OSSAccessKeyId=mock&Signature=mock` 的签名 URL」。那是 OSS 时代的
遗留契约，已被端点推翻 —— audio_url 现在多是**永久静态直链**（bucket 公共读 /
OSS_PUBLIC_BASE_URL，内容归属 app 账户、必须长期可播），对它做两件错事：

  1. 拼上无意义的 mock 签名（SeaweedFS 靠「忽略未知参数」才没报错）；
  2. 回报假的 `expires_at`，让客户端以为会过期 → 每次恢复播放/切档都白跑一次
     audio-url 接口（App 侧 isExpiringSoon() 会命中）。

现行契约（见 content-service/main.py 的 article_audio_url docstring）：
  - URL **本来不带 query** → 静态直链，原样返回，`expires_at=None`（不会过期）
  - URL **本来就带 query**   → 已签名，原样返回并回报 `expires_at`

所以「ready 返回签名 URL」这条用例改成断言静态直链被原样透传，并另加一条
覆盖预签名分支——那条分支此前完全没有用例，是这次换契约时被漏掉的。
"""

from datetime import datetime, timezone

from helpers import client, new_article, new_task, new_user

OSS_AUDIO = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def audio_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}/audio-url"


async def _new_ready_article(uid: int, stored_url: str | None = None) -> str:
    article_id = await new_article(uid, status="distilling")
    await new_task(
        article_id,
        status="done",
        audio_url=stored_url or f"{OSS_AUDIO}/{article_id}.m4a",
        duration_sec=300,
    )
    return article_id


# ---------------------------------------------------------------------------
# 1. ready + 静态直链 → 原样返回，不拼签名，expires_at=None
# ---------------------------------------------------------------------------
async def test_audio_url_ready_returns_static_url_unchanged():
    uid, token = await new_user()
    article_id = await _new_ready_article(uid)

    async with client(token) as c:
        r = await c.get(audio_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    assert body["duration_sec"] == 300
    # 静态直链必须原样透传：一个签名参数都不许被拼上去
    assert body["audio_url"] == f"{OSS_AUDIO}/{article_id}.m4a"
    assert "?" not in body["audio_url"]
    assert "OSSAccessKeyId" not in body["audio_url"]
    assert "Signature" not in body["audio_url"]
    # 不会过期 → 客户端据此不再做「过期前刷新」
    assert body["expires_at"] is None


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
# 5. 库里存的 URL 本来就带签名参数 → 原样返回，并回报 expires_at
#
# 这条分支在 CP-AUDIO-URL-STATIC 之前不存在（旧实现是无条件拼 mock 签名），
# 换契约时被漏掉了。静态直链不校验过期，只有已签名 URL 才需要。
# ---------------------------------------------------------------------------
async def test_audio_url_presigned_returns_expires_at():
    uid, token = await new_user()
    presigned = f"{OSS_AUDIO}/art_presigned.m4a?Expires=9999999999&OSSAccessKeyId=ak&Signature=sig"
    article_id = await _new_ready_article(uid, stored_url=presigned)

    before = datetime.now(timezone.utc).timestamp()
    async with client(token) as c:
        r = await c.get(audio_url(article_id))
    assert r.status_code == 200, r.text

    body = r.json()
    # 已签名 URL 原样透传，不被改写
    assert body["audio_url"] == presigned
    # 客户端据此判断「快过期了，去续签」
    expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
    assert expires_at > before + 3600 - 5  # 容忍 5s 误差
    assert expires_at <= before + 3600 + 5


# ---------------------------------------------------------------------------
# 6. 本地模式的兜底 URL 解析：认得内容寻址的文件名（2026-10-22 修）
#
# 本地模式（STORAGE_PROVIDER=local）下，兜底 URL 是按 `{article_id}.{ext}`
# 拼出来的，而磁盘上真实存在的文件是**内容寻址**命名
# `audio/{article_id}.{hash8}.{ext}`（见 app/services/storage/base.py）。
# 旧实现只认无 hash 的旧命名 → 兜底永远解析不到真实产物（返回的 URL 指向
# 一个不存在的文件）；而当时之所以"能用"，是因为 TTS 工具还往同一个目录
# 里写了一份无 hash 的重复副本 —— 那份重复副本正是 2026-10-22 发现并修掉的
# 「同一份音频存两遍」问题。修掉重复之后，兜底解析必须跟着认新命名。
# ---------------------------------------------------------------------------


def test_resolve_actual_extension_finds_versioned_file(tmp_path):
    """兜底解析要认内容寻址的文件名、取**最新**的那个、且不认多码率变体。"""
    import os
    import time

    from helpers import content_main

    audio_dir = tmp_path / "data"
    art_dir = audio_dir / "audio"
    art_dir.mkdir(parents=True)

    old = art_dir / "art_x.11111111.mp3"
    new = art_dir / "art_x.22222222.mp3"
    variant = art_dir / "art_x.64k.33333333.m4a"
    for f in (old, new, variant):
        f.write_bytes(b"x" * 10)
    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(new, (now, now))
    os.utime(variant, (now + 10, now + 10))  # 变体比主档还新，也不能被选中

    base = "https://api.example.com/audio/audio/art_x.mp3"
    resolved = content_main._resolve_actual_audio_extension(base, str(audio_dir))

    assert resolved.endswith(
        "/audio/audio/art_x.22222222.mp3"
    ), f"解析到了 {resolved}，应是最新主档 art_x.22222222.mp3"
    assert "64k" not in resolved, "不该指向多码率变体"


def test_resolve_actual_extension_prefers_plain_when_only_plain_exists(tmp_path):
    """历史命名（无 hash）仍然要认（老数据没有内容寻址版本）。"""
    from helpers import content_main

    audio_dir = tmp_path / "data"
    (audio_dir / "audio").mkdir(parents=True)
    (audio_dir / "audio" / "art_y.mp3").write_bytes(b"x" * 10)

    base = "https://api.example.com/audio/audio/art_y.mp3"
    assert content_main._resolve_actual_audio_extension(base, str(audio_dir)).endswith(
        "/audio/audio/art_y.mp3"
    )
