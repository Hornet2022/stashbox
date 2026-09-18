"""CP5.2 用户端 distill 失败重试 API 单测（v1 §11.5）。

覆盖：
  POST /api/v1/articles/{id}/retry
    1. test_retry_failed_article_succeeds        — failed → pending + retry_count+1
    2. test_retry_not_found_returns_404         — 不存在的 article
    3. test_retry_other_user_article_returns_403 — 不属于当前 user
    4. test_retry_already_pending_returns_409   — 状态 pending 不能重试
    5. test_retry_already_ready_returns_409     — 状态 ready 不能重试
    6. test_retry_writes_push_notification      — 写 push_notifications 表

依赖真实 PG（/tmp:5432）+ alembic upgrade head（0009）。
"""
import pytest
from sqlalchemy import select

from stashbox.backend.common.models.push_notification import PushNotification

from helpers import client, new_article, new_user


@pytest.mark.asyncio
async def test_retry_failed_article_succeeds(fake_ai_client):
    """正常路径：failed → pending，retry_count 自增，触发蒸馏。"""
    uid, token = await new_user()
    art_id = await new_article(uid, status="failed")

    async with client(token) as c:
        resp = await c.post(f"/api/v1/articles/{art_id}/retry")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["article_id"] == art_id
    assert body["status"] == "pending"
    assert body["retry_count"] == 1
    assert body["distill_triggered"] is True
    assert "queued_at" in body
    # 触发了 distill
    assert len(fake_ai_client.calls) == 1
    assert fake_ai_client.calls[0]["article_id"] == art_id


@pytest.mark.asyncio
async def test_retry_not_found_returns_404(fake_ai_client):
    """文章不存在 → 404，不触发蒸馏。"""
    uid, token = await new_user()

    async with client(token) as c:
        resp = await c.post("/api/v1/articles/art_not_exist_404/retry")

    assert resp.status_code == 404
    assert fake_ai_client.calls == []


@pytest.mark.asyncio
async def test_retry_other_user_article_returns_403(fake_ai_client):
    """article 不属于当前 user → 403。"""
    uid1, token1 = await new_user()
    _uid2, token2 = await new_user()
    art_id = await new_article(uid1, status="failed")

    async with client(token2) as c:
        resp = await c.post(f"/api/v1/articles/{art_id}/retry")

    assert resp.status_code == 403
    assert fake_ai_client.calls == []


@pytest.mark.asyncio
async def test_retry_already_pending_returns_409(fake_ai_client):
    """状态 pending → 409。"""
    uid, token = await new_user()
    art_id = await new_article(uid, status="pending")

    async with client(token) as c:
        resp = await c.post(f"/api/v1/articles/{art_id}/retry")

    assert resp.status_code == 409
    assert "pending" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_already_ready_returns_409(fake_ai_client):
    """状态 ready → 409。"""
    uid, token = await new_user()
    art_id = await new_article(uid, status="ready")

    async with client(token) as c:
        resp = await c.post(f"/api/v1/articles/{art_id}/retry")

    assert resp.status_code == 409
    assert "ready" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_writes_push_notification(fake_ai_client):
    """重试成功后写一条 push_notifications 记录。"""
    uid, token = await new_user()
    art_id = await new_article(uid, status="failed")

    async with client(token) as c:
        resp = await c.post(f"/api/v1/articles/{art_id}/retry")

    assert resp.status_code == 200
    # 验证 push_notifications 表有对应记录
    from stashbox.backend.common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(PushNotification).where(
                PushNotification.user_id == uid,
                PushNotification.article_id == art_id,
            )
        )
        notif = result.scalar_one_or_none()
        assert notif is not None
        assert notif.title == "换个来源重试？"
        assert "拒收" in notif.body or "换一个源" in notif.body
