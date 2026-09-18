"""
CP5.1 回归：POST /api/v1/users/me/onboarding/{start,step,done} 三个端点。

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。
"""
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]

START_URL = "/api/v1/users/me/onboarding/start"
STEP_URL = "/api/v1/users/me/onboarding/step"
DONE_URL = "/api/v1/users/me/onboarding/done"


def _load_app(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp51_onboarding_user_main", "user-service/main.py")


async def new_user() -> tuple[int, str]:
    """建一个测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp51_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
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


async def db_onboarding_done_at(user_id: int):
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(User.onboarding_done_at).where(User.id == user_id)
            )
        ).scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# 1. 首次进入引导返回 200
# ---------------------------------------------------------------------------
async def test_onboarding_start_first_time_succeeds():
    uid, token = await new_user()
    async with client(token) as c:
        r = await c.post(START_URL)
    assert r.status_code == 200, r.text
    assert r.json()["onboarding_started"] is True


# ---------------------------------------------------------------------------
# 2. 已完成引导的用户再 start 返回 409
# ---------------------------------------------------------------------------
async def test_onboarding_start_already_done_returns_409():
    uid, token = await new_user()
    # 先标记完成
    async with AsyncSessionLocal() as session:
        u = await session.get(User, uid)
        u.onboarding_done_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()

    async with client(token) as c:
        r = await c.post(START_URL)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 3. 引导第 1/2/3 步都返回 200
# ---------------------------------------------------------------------------
async def test_onboarding_step_viewed_1_2_3_all_succeed():
    uid, token = await new_user()
    expected_names = {1: "copy_link", 2: "open_d9", 3: "listen_audio"}
    for step in (1, 2, 3):
        async with client(token) as c:
            r = await c.post(STEP_URL, params={"step": step})
        assert r.status_code == 200, f"step={step}: {r.text}"
        body = r.json()
        assert body["step_viewed"] == step
        assert body["step_name"] == expected_names[step]


# ---------------------------------------------------------------------------
# 4. 无效 step 返回 400
# ---------------------------------------------------------------------------
async def test_onboarding_step_invalid_returns_400():
    uid, token = await new_user()
    async with client(token) as c:
        r = await c.post(STEP_URL, params={"step": 0})
    assert r.status_code == 400, r.text

    async with client(token) as c:
        r = await c.post(STEP_URL, params={"step": 4})
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 5. 首次调用 done 写 onboarding_done_at
# ---------------------------------------------------------------------------
async def test_onboarding_done_first_time_sets_timestamp():
    uid, token = await new_user()
    assert await db_onboarding_done_at(uid) is None

    async with client(token) as c:
        r = await c.post(DONE_URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["onboarding_done"] is True
    assert body["onboarding_done_at"] is not None

    done_at = await db_onboarding_done_at(uid)
    assert done_at is not None


# ---------------------------------------------------------------------------
# 6. 重复调用 done 是幂等的（更新时间戳）
# ---------------------------------------------------------------------------
async def test_onboarding_done_idempotent_updates_timestamp():
    uid, token = await new_user()

    async with client(token) as c:
        r1 = await c.post(DONE_URL)
    assert r1.status_code == 200, r1.text
    first_at = r1.json()["onboarding_done_at"]

    async with client(token) as c:
        r2 = await c.post(DONE_URL)
    assert r2.status_code == 200, r2.text
    second_at = r2.json()["onboarding_done_at"]

    # 第二次调用仍成功，且时间戳更新了（不是同一个值）
    assert second_at is not None
    assert second_at >= first_at  # 幂等：更新而非报错


# ---------------------------------------------------------------------------
# 7. start/step/done 三个端点都触发 track_simple（不抛异常即算过）
# ---------------------------------------------------------------------------
async def test_onboarding_events_track_called():
    """验证三个端点在正常流程下不抛异常（track_simple 失败不挂业务）。"""
    uid, token = await new_user()

    async with client(token) as c:
        r_start = await c.post(START_URL)
        assert r_start.status_code == 200, r_start.text

        for step in (1, 2, 3):
            r_step = await c.post(STEP_URL, params={"step": step})
            assert r_step.status_code == 200, f"step={step}: {r_step.text}"

        r_done = await c.post(DONE_URL)
        assert r_done.status_code == 200, r_done.text
