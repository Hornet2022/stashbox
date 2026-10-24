"""push_notifications 表 + 拉取/mark-read 端点（CP5.4a）。v1 §11.5 CP5.4。

前置：本机 PG 5432 已起，且已 alembic upgrade 0008。
真推送（极光/友盟）CP4.6 范围；蒸馏触发推送 CP5.4b 范围。
"""

import importlib.util
import pytest
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp54a_notif_user_main", "user-service/main.py")


async def new_user() -> tuple[int, str]:
    """建一个测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp54a_" + uuid.uuid4().hex[:24],
            nickname="pytest_cp54a",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = create_access_token(str(user.id))
        return user.id, token


async def _new_article(user_id: int) -> str:
    """造一篇真实的文章（push_notifications.article_id 有外键指向 articles）。"""
    from stashbox.backend.common.models import Article

    article_id = "art_" + uuid.uuid4().hex[:24]
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=article_id,
                user_id=user_id,
                url=f"https://example.com/{article_id}",
                title="测试文章",
                source="web",
                raw_content={"content_text": "正文", "source": "web"},
            )
        )
        await s.commit()
    return article_id


@pytest.mark.asyncio
async def test_push_notifications_table_exists():
    """alembic 0008 后表存在"""
    async with AsyncSessionLocal() as s:
        # 即使表空，SELECT 1 FROM ... LIMIT 1 不报 ProgrammingError 即存在
        await s.execute(text("SELECT 1 FROM push_notifications LIMIT 1"))


@pytest.mark.asyncio
async def test_list_notifications_endpoint():
    """GET /api/v1/notifications 端点存在 + 返 200"""
    user_id, token = await new_user()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/notifications",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "notifications" in data
    assert "unread_count" in data


@pytest.mark.asyncio
async def test_mark_read_endpoint():
    """POST /api/v1/notifications/{id}/mark-read 端点存在 + 返 200"""
    user_id, token = await new_user()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        # 404 on non-existent notification
        resp = await client.post(
            "/api/v1/notifications/99999/mark-read",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 「已投递」回报端点（2026-10-24）
# ---------------------------------------------------------------------------
#
# 没有厂商推送通道时，App 自己拉通知中心 → 发本地系统通知 → 回报本端点。
# 与 mark-read 是**两件事**：投递 ≠ 用户看过。
@pytest.mark.asyncio
async def test_delivered_endpoint_存在且回报后状态变sent():
    """回报「已投递」⇒ status='sent'，但**不动 read_at**（角标不能被清零）。"""
    from stashbox.backend.common.models import PushNotification

    user_id, token = await new_user()
    async with AsyncSessionLocal() as s:
        notif = PushNotification(
            user_id=user_id,
            article_id=await _new_article(user_id),
            tag_slug="tech",
            title="新文章：测试",
            body="你订阅的 科技 标签有新文章蒸馏完成",
            deeplink="stashbox://detail/art_delivered_test",
            status="failed",
            error="未配置推送通道（设置 STASHBOX_PUSH_WEBHOOK_URL 后接入）",
        )
        s.add(notif)
        await s.commit()
        await s.refresh(notif)
        notif_id = notif.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/notifications/{notif_id}/delivered",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"

    async with AsyncSessionLocal() as s:
        row = await s.get(PushNotification, notif_id)
        assert row.status == "sent"
        assert row.sent_at is not None
        assert row.error is None
        # ⚠️ **read_at 不能动** —— 用户还没看过，角标不能被清零
        assert row.read_at is None


@pytest.mark.asyncio
async def test_delivered_endpoint_不能操作他人的通知():
    """越权防护：不能回报别人的通知。"""
    from stashbox.backend.common.models import PushNotification

    owner_id, _ = await new_user()
    other_id, other_token = await new_user()
    async with AsyncSessionLocal() as s:
        notif = PushNotification(
            user_id=owner_id,
            article_id=await _new_article(owner_id),
            tag_slug="tech",
            title="别人的通知",
            body="body",
            status="failed",
        )
        s.add(notif)
        await s.commit()
        await s.refresh(notif)
        notif_id = notif.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/notifications/{notif_id}/delivered",
            headers={"Authorization": f"Bearer {other_token}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delivered_endpoint_幂等():
    """重复回报不报错（App 可能重试）。"""
    from stashbox.backend.common.models import PushNotification

    user_id, token = await new_user()
    async with AsyncSessionLocal() as s:
        notif = PushNotification(
            user_id=user_id,
            article_id=await _new_article(user_id),
            tag_slug="tech",
            title="幂等",
            body="body",
            status="pending",
        )
        s.add(notif)
        await s.commit()
        await s.refresh(notif)
        notif_id = notif.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        r1 = await client.post(
            f"/api/v1/notifications/{notif_id}/delivered",
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = await client.post(
            f"/api/v1/notifications/{notif_id}/delivered",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
