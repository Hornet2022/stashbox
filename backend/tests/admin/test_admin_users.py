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
    # role 不是独立字段：后端 user-service/main.py:873 是
    #   role = tier if tier in {"admin","operator"} else "user"
    # 所以 free 用户拿到的 role 是 "user" 而不是 "free"。
    # 原来这里断言 role == tier，等于把「某个函数的输出 == 它的输入」
    # 这个假想当成了契约 —— 既不成立，也不表达任何产品含义。
    assert item["tier"] == "free"
    assert item["role"] == "user"
    assert item["status"] == "active"


@pytest.mark.asyncio
async def test_list_users_pagination():
    """分页：两页不重叠，**且 created_at 相同也能分页**。

    2026-10-22 修的 bug：列表排序只按 `created_at DESC`，**没有唯一 tiebreaker**
    → 同一时刻创建的多个用户并列时，PostgreSQL 对并列元素的排序**不稳定**
    → 同一个用户会同时出现在第 1 页和第 2 页，另一个人两页都看不到
    （实测撞过：`assert not (set(ids1) & set(ids2))` → `{46762} & {46762}`）。

    这个测试**故意**造出并列：三个用户 created_at 完全相同。
    修好后排序是全序（created_at DESC, id DESC）→ 翻页结果稳定可复现。
    """
    tied: list[int] = []
    async with AsyncSessionLocal() as s:
        for i in range(3):
            u = User(
                open_id=f"page_tie_{uuid.uuid4().hex[:24]}",
                nickname=f"page_tie_user_{i}",
                tier="free",
            )
            s.add(u)
            await s.flush()
            tied.append(int(u.id))
        await s.commit()

    # 把三行的 created_at 改成**完全相同**的值（且等于全表最大值 → 它们排在最前）。
    #
    # ⚠️ 不要在 Python 侧算「将来 1 小时」再塞进去：created_at 是
    # timestamp with time zone，naive 值会按**数据库会话时区**（本机 UTC+8）
    # 解释 → 「+1 小时」实际变成「-7 小时」→ 反而排到最后（实测踩过）。
    # 让 DB 自己算 now() 最稳。
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE users SET created_at = (SELECT max(created_at) FROM users) "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": tied},
        )
        await s.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        h = {"Authorization": f"Bearer {_token(tied[0])}"}
        pages = []
        for page in (1, 2, 3):
            resp = await client.get(f"/api/v1/admin/users?page={page}&size=1", headers=h)
            assert resp.status_code == 200, resp.text
            pages.append([it["id"] for it in resp.json()["items"]])

    ids = [i for p in pages for i in p]
    # ① 三个用户一个都不重（修复前这里会撞上重复 id）
    assert len(ids) == len(set(ids)), f"分页出现重复用户：{ids}"
    # ② 三个并列的用户**一个都不能漏**（修复前可能有一个人两页都看不到）
    assert set(tied).issubset(set(ids)), f"并列的用户没被完整翻出来：缺 {set(tied) - set(ids)}"


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
async def test_quota_adjust_negative_rejected():
    """monthly_quota < 0 -> 400。"""
    target = await _make_user()
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": -1, "reason": "vip 客户补偿"},
        )
    assert resp.status_code == 400


async def test_quota_adjust_zero_is_allowed_on_purpose():
    """monthly_quota == 0 是**合法值**，不是非法输入。

    0 的语义是「额度耗尽、停用该用户」—— 后端 user-service/main.py 的校验
    是 `if req.monthly_quota < 0`，并且跨端 E2E 实测就是靠调成 0 来停用用户的
    （调高/停用都走这一个端点）。原来这里断言 0 返 400，那是这条规则改成
    「< 0 才拒」之前的旧契约。
    """
    target = await _make_user()
    admin = await _make_user(tier="admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/users/{target}/quota-adjust",
            headers={"Authorization": f"Bearer {_token(admin)}"},
            json={"monthly_quota": 0, "reason": "违规停用该用户"},
        )
    assert resp.status_code == 200
    assert resp.json()["monthly_quota"] == 0


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
