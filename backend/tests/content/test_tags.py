"""tags 表 + GET /api/v1/tags DB 化（CP5.3a）。v1 §11.5 CP5.3 后端基础。

前置条件：alembic upgrade 0007 已跑（dev DB 升级由 Hornet 手动执行）。
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Tag, TagSubscription


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _unique_slug():
    return f"test_tag_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# CP5.3a existing tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tags_table_exists():
    """alembic 0007 后 tags 表存在 + 4 个 seed（tech/finance/life/news）"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT slug, name, category FROM tags ORDER BY slug"))
        rows = r.fetchall()
        assert len(rows) >= 4, f"tags 表只有 {len(rows)} 行（期望 ≥ 4 seed）"


@pytest.mark.asyncio
async def test_tag_subscriptions_table_exists():
    """tag_subscriptions 表存在"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT 1 FROM tag_subscriptions LIMIT 1"))
        # 不管返不返 rows，只确认表存在
        # 如果表不存在 SQLAlchemy 抛 ProgrammingError


# ---------------------------------------------------------------------------
# CP5.3b new tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_create_tag():
    """admin create 端点：mock user 创 tag（幂等 slug → 409）"""
    from stashbox.backend.content_service.main import app

    slug = _unique_slug()
    mock_user = {"id": 999, "tier": "admin"}

    with patch("stashbox.backend.content_service.main.require_admin_or_operator") as mock_auth:
        mock_auth.return_value = mock_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tags",
                json={"slug": slug, "name": "测试标签", "category": "test"},
            )
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["id"] == slug
        assert data["name"] == "测试标签"
        assert data["category"] == "test"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_admin_create_tag_duplicate_409():
    """重复 slug → 409"""
    from stashbox.backend.content_service.main import app

    slug = _unique_slug()
    mock_user = {"id": 999, "tier": "admin"}

    # 创建第一个
    with patch("stashbox.backend.content_service.main.require_admin_or_operator") as mock_auth:
        mock_auth.return_value = mock_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/tags", json={"slug": slug, "name": "标签A"})
            # 重复创建
            resp = await client.post("/api/v1/tags", json={"slug": slug, "name": "标签B"})

        assert resp.status_code == 409, f"期望 409，实际 {resp.status_code}: {resp.text}"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_user_subscribe_tag_idempotent():
    """subscribe 端点：幂等（重复订阅返 already_subscribed）+ 写 tag_subscriptions"""
    from stashbox.backend.content_service.main import app

    slug = _unique_slug()
    user_id = 1001

    # 先建一个 tag
    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name="sub_test", category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()
        tag_id = t.id

    mock_user = {"id": user_id, "sub": str(user_id)}

    with patch("stashbox.backend.content_service.main.require_user") as mock_auth:
        mock_auth.return_value = mock_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 第一次订阅
            resp1 = await client.post(f"/api/v1/tags/{slug}/subscribe")
            assert resp1.status_code == 200, f"第一次订阅失败: {resp1.text}"
            data1 = resp1.json()
            assert data1["ok"] is True
            assert "already_subscribed" not in data1

            # 第二次订阅（幂等）
            resp2 = await client.post(f"/api/v1/tags/{slug}/subscribe")
            assert resp2.status_code == 200, f"幂等订阅失败: {resp2.text}"
            data2 = resp2.json()
            assert data2["ok"] is True
            assert data2.get("already_subscribed") is True

            # 用数字 id 也行
            resp3 = await client.post(f"/api/v1/tags/{tag_id}/subscribe")
            assert resp3.status_code == 200
            assert resp3.json().get("already_subscribed") is True

    # verify DB
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(TagSubscription).where(
                TagSubscription.user_id == user_id,
                TagSubscription.tag_id == tag_id,
            )
        )
        sub = r.scalar_one_or_none()
        assert sub is not None, "tag_subscriptions 应该有一行"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_user_unsubscribe_tag_idempotent():
    """unsubscribe 端点：幂等（本来没订阅也返 already_unsubscribed）+ 删 tag_subscriptions"""
    from stashbox.backend.content_service.main import app

    slug = _unique_slug()
    user_id = 1002

    # 先建一个 tag
    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name="unsub_test", category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()
        tag_id = t.id

    mock_user = {"id": user_id, "sub": str(user_id)}

    with patch("stashbox.backend.content_service.main.require_user") as mock_auth:
        mock_auth.return_value = mock_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 本来就没订阅 → already_unsubscribed
            resp1 = await client.post(f"/api/v1/tags/{slug}/unsubscribe")
            assert resp1.status_code == 200, f"未订阅取消失败: {resp1.text}"
            data1 = resp1.json()
            assert data1["ok"] is True
            assert data1.get("already_unsubscribed") is True

            # 订阅后再取消
            await client.post(f"/api/v1/tags/{slug}/subscribe")
            resp2 = await client.post(f"/api/v1/tags/{slug}/unsubscribe")
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["ok"] is True
            assert "already_unsubscribed" not in data2

            # 数字 id 也可
            resp3 = await client.post(f"/api/v1/tags/{tag_id}/unsubscribe")
            assert resp3.status_code == 200
            assert resp3.json().get("already_unsubscribed") is True

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()
