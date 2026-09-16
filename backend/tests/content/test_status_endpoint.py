"""
CP1.7 status 端点单测：GET /api/v1/articles/{id}/status（6 个 case）。

覆盖：pending / distilling / ready / failed 四种状态 + 非 owner 403 + 不存在 404。
"""
from helpers import client, new_article, new_task, new_user

OSS_AUDIO = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def status_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}/status"


async def _new_ready_article(uid: int) -> tuple[str, str]:
    article_id = await new_article(uid, status="distilling")
    task_id = await new_task(
        article_id,
        status="done",
        audio_url=f"{OSS_AUDIO}/{article_id}.m4a",
        duration_sec=300,
        tags=["科技", "商业"],
        quality_score=8.5,
    )
    return article_id, task_id


# ---------------------------------------------------------------------------
# 1. pending：无蒸馏任务 → task_id / task_status 都是 None
# ---------------------------------------------------------------------------
async def test_status_pending_without_task():
    uid, token = await new_user()
    article_id = await new_article(uid, status="pending")

    async with client(token) as c:
        r = await c.get(status_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"] == article_id
    assert body["status"] == "pending"
    assert body["task_id"] is None
    assert body["task_status"] is None
    assert body["audio_url"] is None
    assert body["error"] is None


# ---------------------------------------------------------------------------
# 2. distilling：task 在跑 → task_status=running，还没有音频
# ---------------------------------------------------------------------------
async def test_status_distilling_with_running_task():
    uid, token = await new_user()
    article_id = await new_article(uid, status="distilling")
    task_id = await new_task(article_id, status="running")

    async with client(token) as c:
        r = await c.get(status_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "distilling"
    assert body["task_id"] == task_id
    assert body["task_status"] == "running"
    assert body["audio_url"] is None


# ---------------------------------------------------------------------------
# 3. ready：task done → 派生 status=ready + audio_url + tags + quality_score
# ---------------------------------------------------------------------------
async def test_status_ready_returns_audio_url():
    uid, token = await new_user()
    article_id, task_id = await _new_ready_article(uid)

    async with client(token) as c:
        r = await c.get(status_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"  # articles.status 停在 distilling，按任务派生
    assert body["task_id"] == task_id
    assert body["task_status"] == "done"
    assert body["audio_url"] == f"{OSS_AUDIO}/{article_id}.m4a"
    assert body["audio_duration_sec"] == 300
    assert body["tags"] == ["科技", "商业"]
    assert body["quality_score"] == 8.5


# ---------------------------------------------------------------------------
# 4. failed：返回失败原因 + task_status=failed
# ---------------------------------------------------------------------------
async def test_status_failed_returns_error():
    uid, token = await new_user()
    article_id = await new_article(uid, status="failed", error="tts timeout")
    await new_task(article_id, status="failed")

    async with client(token) as c:
        r = await c.get(status_url(article_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["task_status"] == "failed"
    assert body["error"] == "tts timeout"
    assert body["audio_url"] is None


# ---------------------------------------------------------------------------
# 5. 非 owner → 403
# ---------------------------------------------------------------------------
async def test_status_not_owner_returns_403():
    _uid, _token = await new_user()
    other_uid, other_token = await new_user()
    article_id = await new_article(_uid, status="pending")

    async with client(other_token) as c:
        r = await c.get(status_url(article_id))

    assert r.status_code == 403, r.text
    assert r.json()["code"] == 40300
    assert other_uid != _uid


# ---------------------------------------------------------------------------
# 6. 文章不存在 → 404
# ---------------------------------------------------------------------------
async def test_status_not_found_returns_404():
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.get(status_url("art_not_exist"))

    assert r.status_code == 404, r.text
    assert r.json()["code"] == 40400
