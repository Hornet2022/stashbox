"""CP3.6.2-XIN admin login 端点（v1 §3.6）。

覆盖：
  POST /api/v1/admin/auth/login
  - 正常 admin 登录 → 200 + JWT（payload 含 sub/role/exp/iat，且写一条 ADMIN_LOGIN 审计）
  - 正常 operator 登录 → 200 + JWT
  - 错误密码 → 401
  - 不存在用户 → 401
  - 普通 user(free) 角色 → 403
  - 缺 email → 400
  - 缺 password → 400

注意：本地 alembic 卡在 0004，users 表无 email/password_hash 列。
fixture 用 create_all + ALTER TABLE ADD COLUMN IF NOT EXISTS 幂等补齐，
绕过 broken 迁移链（同 test_admin_users.py 思路）。
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import bcrypt
import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import decode_token
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


user_app = _load_app("_cp362xin_admin_login_main", "user-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_schema():
    """幂等建 users + admin_operation_logs，并补齐 email/password_hash 列。"""
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, AdminOperationLog.__table__],
        )
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)"))
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
        )
    yield


async def _make_user(
    email: str, password: str, tier: str = "admin", nickname: str | None = None
) -> int:
    phash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp362xin_" + uuid.uuid4().hex[:24],
            email=email,
            password_hash=phash,
            tier=tier,
            nickname=nickname or email,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _login(email: str, password: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/v1/admin/auth/login", json={"email": email, "password": password}
        )


@pytest.mark.asyncio
async def test_admin_login_ok():
    """正常 admin 登录 → 200 + JWT；payload 含 sub/role/exp/iat；写一条 ADMIN_LOGIN 审计。"""
    email = "admin_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    uid = await _make_user(email, "admin_secret_123", tier="admin", nickname="Hornet")
    resp = await _login(email, "admin_secret_123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert "access_token" in body and body["access_token"]

    # JWT payload 校验
    payload = decode_token(body["access_token"])
    assert payload["sub"] == str(uid)
    assert payload["role"] == "admin"
    assert "exp" in payload and "iat" in payload
    assert payload["exp"] - payload["iat"] == 3600

    # user 字段
    u = body["user"]
    assert u["id"] == uid and u["email"] == email and u["role"] == "admin"

    # 审计日志
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id = :tid AND action='ADMIN_LOGIN'"
            ),
            {"tid": str(uid)},
        )
        assert cnt == 1


@pytest.mark.asyncio
async def test_operator_login_ok():
    """正常 operator 登录 → 200 + JWT（role=operator）。"""
    email = "operator_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email, "op_secret_123", tier="operator", nickname="Op")
    resp = await _login(email, "op_secret_123")
    assert resp.status_code == 200
    body = resp.json()
    payload = decode_token(body["access_token"])
    assert payload["role"] == "operator"
    assert body["user"]["role"] == "operator"


@pytest.mark.asyncio
async def test_wrong_password():
    """密码错误 → 401，且不写审计日志。"""
    email = "admin_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    uid = await _make_user(email, "right_pw", tier="admin")
    resp = await _login(email, "wrong_pw")
    assert resp.status_code == 401
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text("SELECT count(*) FROM admin_operation_logs WHERE target_id = :tid"),
            {"tid": str(uid)},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_user_not_found():
    """不存在用户 → 401。"""
    resp = await _login("nobody_" + uuid.uuid4().hex[:8] + "@stashbox.dev", "x")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_free_role_forbidden():
    """普通 user(free) 角色（密码正确）→ 403。"""
    email = "free_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email, "free_pw", tier="free", nickname="Free")
    resp = await _login(email, "free_pw")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_email():
    """缺 email → 400。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/admin/auth/login", json={"password": "x"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_password():
    """缺 password → 400。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "a@b.com"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deleted_account_cannot_login():
    """**已注销（软删）的账号不能登后台** → 401。

    2026-10-22 走查发现：wechat 登录和 refresh 都查了 users.deleted_at，
    只有 admin 登录漏了 —— 结果是「注销账号」这个承诺对后台不生效：
    注销过的 admin/operator 照样拿密码登进管理后台。

    三处登录口径必须一致，判据只有一条：deleted_at is not None → 拒绝。
    """
    from datetime import datetime, timezone

    email = "deleted_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    uid = await _make_user(email, "admin_secret_123", tier="admin")
    async with AsyncSessionLocal() as s:
        u = await s.get(User, uid)
        u.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await s.commit()

    resp = await _login(email, "admin_secret_123")
    assert resp.status_code == 401, f"注销过的账号还能登后台：{resp.text}"


# ---------------------------------------------------------------------------
# 暴力破解限流（2026-10-22 补）
#
# 这个端点原来**没有任何失败计数、没有锁定、没有验证码**，
# 而公开的 wechat-login 反而有 Redis 限流 —— 口径不一致，
# 而后台恰恰是更值得防的那个入口。
#
# 计数语义是「连续失败次数」：所有尝试都计数，但**登录成功就清零**。
# 于是正常使用永远不会被锁（每次成功都归零），
# 而暴力破解时失败数会一路堆上去直到被锁。
# ---------------------------------------------------------------------------


async def _clear_rate_limit(email: str, ip: str = "127.0.0.1") -> None:
    from stashbox.backend.common import rate_limit

    await rate_limit.reset(f"ratelimit:admin_login:email:{email.lower()}")
    await rate_limit.reset(f"ratelimit:admin_login:ip:{ip}")


@pytest.mark.asyncio
async def test_连续失败超限后被锁():
    """连续错 5 次 → 第 6 次直接 429，不再进密码校验。"""
    email = "brute_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email, "right_pw", tier="admin")
    await _clear_rate_limit(email)

    try:
        # 前 5 次：密码错 → 401（还在额度内）
        for i in range(5):
            resp = await _login(email, "wrong_pw")
            assert resp.status_code == 401, f"第 {i + 1} 次应是 401：{resp.text}"

        # 第 6 次：被限流挡住
        resp = await _login(email, "wrong_pw")
        assert resp.status_code == 429, f"连续失败超限后应被锁：{resp.text}"
        assert "429" in resp.text or "频繁" in resp.text
    finally:
        await _clear_rate_limit(email)


@pytest.mark.asyncio
async def test_锁住期间正确密码也进不去():
    """被锁之后即使密码正确也进不去 —— 限流要挡在密码校验**之前**。

    如果限流放在密码校验之后，爆破者可以靠「多试几次总能蒙对」绕过计数。
    """
    email = "brute2_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email, "right_pw", tier="admin")
    await _clear_rate_limit(email)

    try:
        for _ in range(5):
            assert (await _login(email, "wrong_pw")).status_code == 401
        resp = await _login(email, "right_pw")
        assert resp.status_code == 429, "被锁期间正确密码也该被挡住"
    finally:
        await _clear_rate_limit(email)


@pytest.mark.asyncio
async def test_登录成功会清零连续失败计数():
    """成功一次 → 计数归零 → 又有完整额度。

    这条是「正常使用不会被锁」的关键：手滑输错几次的人不该被永久挡在门外。
    """
    email = "ok_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email, "right_pw", tier="admin")
    await _clear_rate_limit(email)

    try:
        for _ in range(4):
            assert (await _login(email, "wrong_pw")).status_code == 401
        # 成功一次 → 清零
        assert (await _login(email, "right_pw")).status_code == 200
        # 清零后又有完整额度
        for _ in range(5):
            assert (await _login(email, "wrong_pw")).status_code == 401
        # 第 6 次才被锁
        assert (await _login(email, "wrong_pw")).status_code == 429
    finally:
        await _clear_rate_limit(email)


@pytest.mark.asyncio
async def test_账号维度互不影响():
    """A 账号的失败次数不该影响 B 账号（账号维度按 email 分开计）。"""
    email_a = "a_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    email_b = "b_" + uuid.uuid4().hex[:8] + "@stashbox.dev"
    await _make_user(email_a, "right_pw", tier="admin")
    await _make_user(email_b, "right_pw", tier="admin")
    await _clear_rate_limit(email_a)
    await _clear_rate_limit(email_b)

    try:
        for _ in range(5):
            assert (await _login(email_a, "wrong_pw")).status_code == 401
        assert (await _login(email_a, "wrong_pw")).status_code == 429
        # B 账号额度独立
        assert (await _login(email_b, "right_pw")).status_code == 200
    finally:
        await _clear_rate_limit(email_a)
        await _clear_rate_limit(email_b)
