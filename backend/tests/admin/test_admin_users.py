"""CP3.6-A2 admin users 2 端点（v1 §3.6 用户管理）。

覆盖：
  GET  /api/v1/admin/users      ：默认 / 分页 / 关键词 / 角色 / 空
  POST /api/v1/admin/users/{id}/quota-adjust：正常 / 用户不存在 / 原因太短 /
                                          quota<=0 / operator 可访问 / free 403

注意：本地 alembic 卡在 0004（0008 push_notifications 的 FK 类型历史 bug，
不在本任务范围），admin_operation_logs（0009）到不了。测试用 ORM metadata
幂等建本测试依赖的两张表（users / admin_operation_logs），不依赖 broken 迁移链。
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import User
from stashbox.backend.common.models.admin_operation_log import AdminOperationLog
from stashbox.backend.common.models.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """user-service 目录名带连字符，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp36a2_admin_user_main", "user-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_tables():
    """幂等建 users + admin_operation_logs（checkfirst），绕过 broken 0008 迁移链。"""
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=[User.__table__, AdminOperationLog.__table__]
        )
    yield


async def _make_user(tier: str = "free", nickname: str | None = None) -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp36a2_" + uuid.uuid4().hex[:24],
            nickname=nickname or ("u_" + uuid.uuid4().hex[:6]),
            tier=tier,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


def _token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


@pytest.mark.asyncio
async def test_list_users_default():
    """默认查询返回 200 + total/items 结构，含新建用户。"""
    uid = await _make_user(nickname="alice_default")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/users", headers={"Authorization": f"Bearer {_token(uid)}"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data and "items" in data
    assert any(it["id"] == uid for it in data["items"])
    # 字段映射：display_name=nickname, role=tier, status=active
    item = next(it for it in data["items"] if it["id"] == uid)
    assert item["display_name"] == "alice_default"
    assert item["role"] == item["tier"] == "free"
    assert item["status"] == "active"


@pytest.mark.asyncio
async def test_list_users_pagination():
    """分页：两页不重叠。"""
    u1 = await _make_user(nickname="page_one_user")
    await _make_user(nickname="page_two_user")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        h = {"Authorization": f"Bearer {_token(u1)}"}
        p1 = await client.get("/api/v1/admin/users?page=1&size=1", headers=h)
        p2 = await client.get("/api/v1/admin/users?page=2&size=1", headers=h)
    assert p1.status_code == 200 and p2.status_code == 200
    ids1 = [it["id"] for it in p1.json()["items"]]
    ids2 = [it["id"] for it in p2.json()["items"]]
    assert len(ids1) == 1
    assert not (set(ids1) & set(ids2))


@pytest.mark.asyncio
async def test_list_users_keyword():
    """关键词：nickname ILIKE 命中 + 反向空。"""
    u = await _make_user(nickname="keyword_zhao")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        h = {"Authorization": f"Bearer {_token(u)}"}
        resp = await client.get("/api/v1/admin/users?keyword=zhao", headers=h)
        resp2 = await client.get("/api/v1/admin/users?keyword=nonexistent_xyz", headers=h)
    assert resp.status_code == 200
    assert any(it["id"] == u for it in resp.json()["items"])
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_users_tier_filter():
    """角色过滤：tier=pro 只返回 pro。"""
    pro = await _make_user(tier="pro", nickname="pro_user_a")
    await _make_user(tier="free", nickname="free_user_b")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/users?tier=pro",
            headers={"Authorization": f"Bearer {_token(pro)}"},
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(it["tier"] == "pro" for it in items)
    assert any(it["id"] == pro for it in items)


@pytest.mark.asyncio
async def test_list_users_empty():
    """空结果：total=0 + items=[]。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/users?keyword=zzz_no_such_user_zzz",
            headers={"Authorization": f"Bearer {_token(1)}"},
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_quota_adjust_normal():
    """正常：更新 monthly_quota + 同事务写一条 audit log。"""
    target = await _make_user(nickname="quota_target", tier="member")
    admin = await _make_user(tier="admin", nickname="quota_admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": 50, "reason": "vip 客户补偿"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == target
    assert data["monthly_quota"] == 50
    assert data["remaining"] == 50
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id = :tid AND action='quota_adjust'"
            ),
            {"tid": str(target)},
        )
        assert cnt == 1


@pytest.mark.asyncio
async def test_quota_adjust_user_not_found():
    """用户不存在 -> 404。"""
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/admin/users/99999999/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": 50, "reason": "vip 客户补偿"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_quota_adjust_reason_too_short():
    """reason < 5 字符 -> 400，且不落库（无 audit 行）。"""
    target = await _make_user()
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": 50, "reason": "abc"},
        )
    assert resp.status_code == 400
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text("SELECT count(*) FROM admin_operation_logs WHERE target_id = :tid"),
            {"tid": str(target)},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_quota_adjust_non_positive():
    """monthly_quota <= 0 -> 400。"""
    target = await _make_user()
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": 0, "reason": "vip 客户补偿"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_quota_adjust_operator_allowed():
    """operator 角色亦可通过 require_admin_or_operator。"""
    target = await _make_user()
    operator = await _make_user(tier="operator")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(operator, tier='operator')}"},
            json={"monthly_quota": 30, "reason": "operator 调整配额"},
        )
    assert resp.status_code == 200
    assert resp.json()["monthly_quota"] == 30


@pytest.mark.asyncio
async def test_admin_requires_role():
    """free 用户访问 admin 端点 -> 403。"""
    free = await _make_user(tier="free")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {_token(free, tier='free')}"},
        )
    assert resp.status_code == 403
