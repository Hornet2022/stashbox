"""CP5.4a-ADMIN：admin 推送队列端点（v1 §端点需求_admin推送队列_v1 §4 验收标准）

覆盖：
  GET /api/v1/admin/push-notifications
    - 无 token → 401
    - free token → 403
    - admin / super_admin / operator → 200
    - status=pending / sent / failed 各只回对应行
    - user_id 只回该用户行
    - tag_slug 只回该标签行
    - 多条件组合（status + user_id）正确
    - limit=2&offset=2 翻页不重不漏，total 为过滤后总数
    - 空结果 → {items:[], total:0}，200
    - status 非法值 → 400
    - limit cap=200（limit=1000 被夹到 200）
    - 不写 admin_operation_logs（只读口径）

依赖真实 PG。push_notifications 表由 fixture autouse 幂等建（绕过 broken 0008 迁移链）。
"""

import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import AdminOperationLog, PushNotification, User
from stashbox.backend.common.models.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """user-service 目录名带连字符，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp54a_admin_push_main", "user-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_tables():
    """幂等建 users + push_notifications + admin_operation_logs（checkfirst）。

    每个 case 起点 TRUNCATE 本测试关心的两张表（push_notifications /
    admin_operation_logs）—— push_notifications 单测场景下其他测试（user-side
    notifications）和 admin 端点共用同一张表,留数据会让 status/total 断言失真。
    TRUNCATE ... RESTART IDENTITY 让自增 id 从 1 重置,避免大数字干扰翻页断言。
    users 不 TRUNCATE：本测试要造大量 _make_user() 自身依赖递增 id;且 users
    表跨测试共享数据是预期行为（admin endpoint 不强依赖 user 唯一性）。
    """
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                PushNotification.__table__,
                AdminOperationLog.__table__,
            ],
        )
    # TRUNCATE 在 autouse 隔 yield 前执行 = 每个 case 起点都是干净状态
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE push_notifications, admin_operation_logs "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


def _token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


async def _make_user(tier: str = "free", nickname: str | None = None) -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp54a_" + uuid.uuid4().hex[:24],
            nickname=nickname or ("u_" + uuid.uuid4().hex[:6]),
            tier=tier,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


async def _make_notification(
    user_id: int,
    *,
    status: str = "sent",
    tag_slug: str | None = None,
    title: str = "今日蒸馏完成",
    error: str | None = None,
    sent_at: datetime | None = None,
) -> int:
    """插一条推送，返回 id。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as s:
        n = PushNotification(
            user_id=user_id,
            article_id=None,
            tag_slug=tag_slug,
            title=title,
            body="《…》已生成,点击收听",
            deeplink=None,
            status=status,
            error=error,
            sent_at=sent_at or now,
            created_at=now,
        )
        s.add(n)
        await s.commit()
        await s.refresh(n)
        return int(n.id)


# ===== 鉴权 =====


@pytest.mark.asyncio
async def test_no_token_401():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/admin/push-notifications")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_free_user_403():
    free = await _make_user(tier="free")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications",
            headers={"Authorization": f"Bearer {_token(free, tier='free')}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_200():
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications",
            headers={"Authorization": f"Bearer {_token(admin, tier='admin')}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) >= {"total", "limit", "offset", "items"}
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_operator_200():
    operator = await _make_user(tier="operator")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications",
            headers={"Authorization": f"Bearer {_token(operator, tier='operator')}"},
        )
    assert resp.status_code == 200


# ===== 过滤 =====


@pytest.mark.asyncio
async def test_status_filter_pending():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u, status="pending", title="P")
    await _make_notification(u, status="sent", title="S")
    await _make_notification(u, status="failed", title="F")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?status=pending",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["status"] == "pending"
    assert data["items"][0]["title"] == "P"


@pytest.mark.asyncio
async def test_status_filter_sent():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u, status="pending")
    await _make_notification(u, status="sent")
    await _make_notification(u, status="sent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?status=sent",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(it["status"] == "sent" for it in data["items"])


@pytest.mark.asyncio
async def test_status_filter_failed():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u, status="failed", error="推送通道 503")
    await _make_notification(u, status="sent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?status=failed",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["error"] == "推送通道 503"


@pytest.mark.asyncio
async def test_status_invalid_value_400():
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?status=banana",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_user_id_filter():
    admin = await _make_user(tier="admin")
    u1 = await _make_user()
    u2 = await _make_user()
    await _make_notification(u1)
    await _make_notification(u1)
    await _make_notification(u2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/v1/admin/push-notifications?user_id={u1}",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(it["user_id"] == u1 for it in data["items"])


@pytest.mark.asyncio
async def test_tag_slug_filter():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u, tag_slug="ai-weekly")
    await _make_notification(u, tag_slug="ai-weekly")
    await _make_notification(u, tag_slug="tech")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?tag_slug=ai-weekly",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert all(it["tag_slug"] == "ai-weekly" for it in items)


@pytest.mark.asyncio
async def test_combined_filter_status_and_user():
    """status + user_id 组合：只回同时满足的行。"""
    admin = await _make_user(tier="admin")
    u1 = await _make_user()
    u2 = await _make_user()
    await _make_notification(u1, status="pending")
    await _make_notification(u1, status="sent")
    await _make_notification(u2, status="pending")  # u2 pending，不该出现
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/v1/admin/push-notifications?status=pending&user_id={u1}",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["user_id"] == u1
    assert data["items"][0]["status"] == "pending"


# ===== 分页 =====


@pytest.mark.asyncio
async def test_pagination_no_overlap_no_miss():
    """limit/offset 翻页不重不漏 + total 为过滤后总数。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    # 插 5 条 sent
    for _ in range(5):
        await _make_notification(u, status="sent")
    # 夹塞 1 条 failed（确保不影响过滤）
    await _make_notification(u, status="failed")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        h = {"Authorization": f"Bearer {_token(admin)}"}
        p1 = await client.get(
            "/api/v1/admin/push-notifications?status=sent&limit=2&offset=0", headers=h
        )
        p2 = await client.get(
            "/api/v1/admin/push-notifications?status=sent&limit=2&offset=2", headers=h
        )
        p3 = await client.get(
            "/api/v1/admin/push-notifications?status=sent&limit=2&offset=4", headers=h
        )
    assert p1.status_code == p2.status_code == p3.status_code == 200
    d1, d2, d3 = p1.json(), p2.json(), p3.json()
    assert d1["total"] == d2["total"] == d3["total"] == 5
    assert d1["limit"] == d2["limit"] == d3["limit"] == 2
    assert d1["offset"] == 0 and d2["offset"] == 2 and d3["offset"] == 4
    ids = [it["id"] for it in d1["items"] + d2["items"] + d3["items"]]
    assert len(ids) == 5
    assert len(set(ids)) == 5  # 不重
    # 排序: created_at DESC,ids 严格递减
    assert ids == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_limit_capped_to_200():
    """limit=1000 被夹到 200（防单次拉爆）。"""
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?limit=1000",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    assert resp.json()["limit"] == 200


@pytest.mark.asyncio
async def test_empty_result_200():
    """无匹配行 → items=[], total=0, 200 不报错。"""
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications?tag_slug=__nonexistent_tag__",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


# ===== 字段口径 =====


@pytest.mark.asyncio
async def test_item_fields_complete():
    """items 单条字段齐全：id/user_id/article_id/tag_slug/title/body/deeplink/status/error/created_at/sent_at/read_at。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u, status="sent", tag_slug="ai-weekly")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/push-notifications",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    expected_keys = {
        "id",
        "user_id",
        "article_id",
        "tag_slug",
        "title",
        "body",
        "deeplink",
        "status",
        "error",
        "created_at",
        "sent_at",
        "read_at",
    }
    assert expected_keys.issubset(set(item.keys()))
    assert item["user_id"] == u
    assert item["status"] == "sent"
    assert item["tag_slug"] == "ai-weekly"
    # 时间字段为 ISO 8601 字符串
    assert isinstance(item["created_at"], str)
    assert isinstance(item["sent_at"], str)


# ===== 副作用 =====


@pytest.mark.asyncio
async def test_no_admin_operation_log_written():
    """GET 不写 admin_operation_logs（只读口径）。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    await _make_notification(u)
    async with AsyncSessionLocal() as s:
        before = await s.scalar(text("SELECT COUNT(*) FROM admin_operation_logs")) or 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        for _ in range(3):
            await client.get(
                "/api/v1/admin/push-notifications",
                headers={"Authorization": f"Bearer {_token(admin)}"},
            )
    async with AsyncSessionLocal() as s:
        after = await s.scalar(text("SELECT COUNT(*) FROM admin_operation_logs")) or 0
    assert after == before, "只读 GET 不应该产生 audit log"


# ===== 网关路由注册（教训口径：漏注册 → 8100 返 404） =====


def test_gateway_routes_include_admin_push_notifications():
    """api-gateway/config.py ROUTES 显式注册 admin/push-notifications（教训口径：漏注册 = 8100 返 404）。"""
    sys.path.insert(0, str(BACKEND_DIR / "api-gateway"))
    spec = importlib.util.spec_from_file_location(
        "_cp54a_admin_push_gw", BACKEND_DIR / "api-gateway" / "config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    matched = [
        r for r in mod.ROUTES if r.method == "GET" and r.path == "/api/v1/admin/push-notifications"
    ]
    assert len(matched) == 1, "网关必须显式注册该端点（admin 段不在 fallback 前缀匹配里）"
    assert matched[0].target_service == "user-service"


def test_gateway_routes_include_admin_push_notifications_retry():
    """POST /admin/push-notifications/{id}/retry 也必须显式注册。

    {notification_id} 是路径参数,如果不放在字面量 GET 之后会被吞掉——教训口径一致。
    """
    sys.path.insert(0, str(BACKEND_DIR / "api-gateway"))
    spec = importlib.util.spec_from_file_location(
        "_cp54a_admin_push_gw_retry", BACKEND_DIR / "api-gateway" / "config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    matched = [
        r
        for r in mod.ROUTES
        if r.method == "POST"
        and r.path == "/api/v1/admin/push-notifications/{notification_id}/retry"
    ]
    assert len(matched) == 1, "网关必须显式注册 retry 端点（admin 段不在 fallback 前缀匹配里）"
    assert matched[0].target_service == "user-service"


# ===== CP5.4a-ADMIN-RETRY：失败推送重推（v1 §端点需求_admin推送队列_v1 §2.3） =====


async def _make_failed_notification(user_id: int, *, error: str = "推送通道 503") -> int:
    """造一条 status=failed 的推送,返回 id。"""
    return await _make_notification(user_id, status="failed", error=error, sent_at=None)


@pytest.mark.asyncio
async def test_retry_failed_notification_success():
    """status=failed 重推成功：status=sent、error=NULL、sent_at 更新、audit 落库。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u, error="推送通道 503")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "运营手动重推恢复线上失败推送"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == nid
    assert data["user_id"] == u
    assert data["status"] == "sent"
    assert data["error"] is None
    assert isinstance(data["sent_at"], str)
    assert isinstance(data["retried_at"], str)
    # 落地校验：DB 行已重置
    async with AsyncSessionLocal() as s:
        row = await s.scalar(
            text(
                "SELECT status||'|'||COALESCE(error,'NULL')||'|'||"
                "CASE WHEN sent_at IS NULL THEN 'NULL' ELSE 'SET' END "
                "FROM push_notifications WHERE id=:i"
            ),
            {"i": nid},
        )
        assert row == "sent|NULL|SET"


@pytest.mark.asyncio
async def test_retry_writes_admin_operation_log():
    """重推写一条 admin_operation_logs(action=retry_push_notification)。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "运营手动重推恢复线上失败推送"},
        )
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT COUNT(*) FROM admin_operation_logs "
                "WHERE target_id=:tid AND action='retry_push_notification'"
            ),
            {"tid": str(nid)},
        )
        assert cnt == 1
        # reason 入库可读（运营复盘）
        reason = await s.scalar(
            text(
                "SELECT reason FROM admin_operation_logs "
                "WHERE target_id=:tid AND action='retry_push_notification'"
            ),
            {"tid": str(nid)},
        )
        assert reason == "运营手动重推恢复线上失败推送"


@pytest.mark.asyncio
async def test_retry_no_token_401():
    failed_nid = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{failed_nid}/retry",
            json={"reason": "缺鉴权重推测试"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_retry_free_user_403():
    free = await _make_user(tier="free")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(free, tier='free')}"},
            json={"reason": "free 角色不能重推"},
        )
    assert resp.status_code == 403
    # 失败不回滚：无 audit log（无副作用已经发生）
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text("SELECT COUNT(*) FROM admin_operation_logs WHERE target_id=:tid"),
            {"tid": str(nid)},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_retry_not_found_404():
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/admin/push-notifications/99999999/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "id 不存在测试"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_sent_status_409():
    """status=sent 不可重推 → 409（已成功无重推意义）。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_notification(u, status="sent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "sent 状态不应被重推"},
        )
    assert resp.status_code == 409
    # 不落库
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text("SELECT COUNT(*) FROM admin_operation_logs WHERE target_id=:tid"),
            {"tid": str(nid)},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_retry_pending_status_409():
    """status=pending 不可重推 → 409（还在队列里,不需重投）。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_notification(u, status="pending", sent_at=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "pending 状态不应被重推"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_reason_missing_400():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        # 没传 reason
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={},
        )
    assert resp.status_code == 400
    # 不落库：状态没动、没 audit
    async with AsyncSessionLocal() as s:
        status = await s.scalar(
            text("SELECT status FROM push_notifications WHERE id=:i"), {"i": nid}
        )
        assert status == "failed"
        cnt = await s.scalar(
            text("SELECT COUNT(*) FROM admin_operation_logs WHERE target_id=:tid"),
            {"tid": str(nid)},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_retry_reason_too_short_400():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "abc"},
        )
    assert resp.status_code == 400
    async with AsyncSessionLocal() as s:
        status = await s.scalar(
            text("SELECT status FROM push_notifications WHERE id=:i"), {"i": nid}
        )
        assert status == "failed"


@pytest.mark.asyncio
async def test_retry_reason_too_long_400():
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "x" * 201},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_retry_already_retried_is_idempotent_blocked():
    """同一行重推第二次会被 409 挡住：第一次成功后 status=sent,第二次不再允许。"""
    admin = await _make_user(tier="admin")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "第一次重推恢复"},
        )
        second = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"reason": "第二次重推应该被拒"},
        )
    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_retry_operator_allowed():
    """operator 角色亦可通过 require_admin_or_operator。"""
    operator = await _make_user(tier="operator")
    u = await _make_user()
    nid = await _make_failed_notification(u)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/push-notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {_token(operator, tier='operator')}"},
            json={"reason": "operator 角色手动重推"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
