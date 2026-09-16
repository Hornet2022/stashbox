"""
CP1.7.3 Bug 2 回归：POST /api/v1/users/me/quota/reset-monthly 返 200 而非 500。

CP1.6 把 `datetime.now(timezone.utc)`（tz-aware）写进 `users.quota_reset_at`，
而 alembic 0002 里该列是 TIMESTAMP WITHOUT TIME ZONE —— asyncpg 直接抛：

    DataError: invalid input for query argument $3:
    datetime.datetime(2026, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)
    (can't subtract offset-naive and offset-aware datetimes)

→ 端点 500。修法：写入前 `.replace(tzinfo=None)`（改列类型留 CP1.8+）。

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]

RESET_URL = "/api/v1/users/me/quota/reset-monthly"
QUOTA_URL = "/api/v1/users/me/quota"


def _load_app(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp173_reset_user_main", "user-service/main.py")


async def new_user_with_usage(used: int = 3, monthly_quota: int = 5) -> tuple[int, str]:
    """建一个已消耗配额的测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp173_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
            monthly_quota=monthly_quota,
            quota_used=used,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return int(user.id), create_access_token(str(user.id))


def client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def db_user(user_id: int) -> tuple[int, int, object]:
    """读 (quota_used, quota_version, quota_reset_at)。"""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(User.quota_used, User.quota_version, User.quota_reset_at).where(
                    User.id == user_id
                )
            )
        ).one()
    return int(row[0]), int(row[1]), row[2]


async def get_quota(token: str) -> dict:
    async with client(token) as c:
        r = await c.get(QUOTA_URL)
        assert r.status_code == 200, r.text
        return r.json()


# ---------------------------------------------------------------------------
# 1. 返 200（不是 500）+ quota_used 清零
# ---------------------------------------------------------------------------
async def test_reset_monthly_returns_200_and_clears_quota_used():
    uid, token = await new_user_with_usage(used=3)
    assert (await db_user(uid))[0] == 3

    async with client(token) as c:
        r = await c.post(RESET_URL)

    assert r.status_code == 200, r.text  # 修前 500（asyncpg DataError）
    body = r.json()
    assert body["reset_users"] >= 1  # 批量重置，至少包含刚建的那个

    used, version, _reset_at = await db_user(uid)
    assert used == 0
    assert version == 1  # 乐观锁版本号 +1


# ---------------------------------------------------------------------------
# 2. quota_reset_at 真的写进去了（且是 naive —— 列是 TIMESTAMP WITHOUT TIME ZONE）
# ---------------------------------------------------------------------------
async def test_reset_monthly_writes_naive_quota_reset_at():
    uid, token = await new_user_with_usage(used=2)

    async with client(token) as c:
        r = await c.post(RESET_URL)

    assert r.status_code == 200, r.text

    _used, _version, reset_at = await db_user(uid)
    assert reset_at is not None  # 修前写入就抛错，整行没动 → 仍是 None
    assert reset_at.tzinfo is None  # naive：列类型没时区
    assert reset_at.day == 1 and reset_at.hour == 0  # 下月 1 号 00:00


# ---------------------------------------------------------------------------
# 3. 幂等：连打两次都 200，第二次不再动这一行
# ---------------------------------------------------------------------------
async def test_reset_monthly_is_idempotent():
    uid, token = await new_user_with_usage(used=4)

    async with client(token) as c:
        first = await c.post(RESET_URL)
        assert first.status_code == 200, first.text
        _used, version_after_first, _ = await db_user(uid)

        second = await c.post(RESET_URL)
        assert second.status_code == 200, second.text

    used, version_after_second, _ = await db_user(uid)
    assert used == 0
    assert version_after_second == version_after_first  # 第二次不再更新这一行


# ---------------------------------------------------------------------------
# 4. 配额接口看到重置后的值（缓存也被失效了，不是旧值）
# ---------------------------------------------------------------------------
async def test_quota_endpoint_reflects_reset_after_cache_invalidation():
    _uid, token = await new_user_with_usage(used=3)

    before = await get_quota(token)
    assert before["quota_used"] == 3

    async with client(token) as c:
        r = await c.post(RESET_URL)
    assert r.status_code == 200, r.text

    after = await get_quota(token)
    assert after["quota_used"] == 0  # 不是缓存里的旧值 3
    assert after["remaining"] == after["monthly_quota"]
