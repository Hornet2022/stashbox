"""
CP1.6 配额扣减事务 + Redis 缓存层 - pytest（6 个 case）。

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。
跑法（PYTHONPATH 需含仓库根的父目录，stashbox 包在那儿）：
    cd /Users/hornet/work/stashbox/backend && PYTHONPATH=/Users/hornet/work pytest tests/test_quota.py -v

覆盖：happy path / 配额用尽 / 并发冲突 / 退还 / 缓存失效 / 缓存 miss。
"""

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import redis
import redis.asyncio
from sqlalchemy import select

from stashbox.backend.common import cache_service
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User
from stashbox.backend.common.redis_client import get_redis_pool

BACKEND_DIR = Path(__file__).resolve().parents[1]

# ai-service 的模块之间是平铺 import（main.py 里 `import dispatcher`），
# 按文件路径加载 main.py 时不会把 ai-service 目录放进 sys.path，
# 于是 `import dispatcher` 失败：ModuleNotFoundError: No module named 'dispatcher'。
# content-service 恰好能加载是因为 main.py 自己会先处理 sys.path。
_AI_DIR = BACKEND_DIR / "ai-service"
if str(_AI_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_DIR))


def _load_app(name: str, rel: str):
    """服务目录名带连字符（content-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


content_app = _load_app("_cp16_content_main", "content-service/main.py")
user_app = _load_app("_cp16_user_main", "user-service/main.py")
ai_app = _load_app("_cp16_ai_main", "ai-service/main.py")


async def new_user(monthly_quota: int = 5) -> tuple[int, str]:
    """建一个测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="test_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
            monthly_quota=monthly_quota,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return int(user.id), create_access_token(str(user.id))


def client(app, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def db_quota_used(user_id: int) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (await session.execute(select(User.quota_used).where(User.id == user_id))).scalar()
        )


async def get_quota(user_id: int, token: str) -> dict:
    async with client(user_app, token) as c:
        r = await c.get("/api/v1/users/me/quota")
        assert r.status_code == 200, r.text
        return r.json()


async def submit(token: str, url: str = "https://example.com/a") -> httpx.Response:
    async with client(content_app, token) as c:
        return await c.post("/api/v1/articles", json={"url": url})


# ---------------------------------------------------------------------------
# 1. happy path：3 次扣减 → quota_used=3
# ---------------------------------------------------------------------------
async def test_happy_path_three_consumes():
    uid, token = await new_user(monthly_quota=5)
    used = []
    for i in range(3):
        r = await submit(token, f"https://example.com/{i}")
        assert r.status_code == 200, r.text
        used.append(r.json()["quota_used"])
    assert used == [1, 2, 3]
    assert await db_quota_used(uid) == 3

    q = await get_quota(uid, token)
    assert q["quota_used"] == 3 and q["monthly_quota"] == 5 and q["remaining"] == 2


# ---------------------------------------------------------------------------
# 2. 配额用尽：quota_used=monthly_quota → POST /articles 抛 3001
# ---------------------------------------------------------------------------
async def test_quota_exceeded_returns_3001():
    _uid, token = await new_user(monthly_quota=2)
    for i in range(2):
        r = await submit(token, f"https://example.com/{i}")
        assert r.status_code == 200, r.text

    r = await submit(token, "https://example.com/overflow")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == 3001  # v1 §3.0 错误码：配额用尽


# ---------------------------------------------------------------------------
# 3. 并发冲突：100 个并发 POST /articles → quota_used == 成功次数
# ---------------------------------------------------------------------------
async def test_concurrent_consume_no_over_sell():
    uid, token = await new_user(monthly_quota=100)
    async with client(content_app, token) as c:
        responses = await asyncio.gather(
            *[
                c.post("/api/v1/articles", json={"url": f"https://example.com/{i}"})
                for i in range(100)
            ]
        )
    success = sum(1 for r in responses if r.status_code == 200)
    assert success > 0
    assert await db_quota_used(uid) == success  # 不超卖、不漏扣


# ---------------------------------------------------------------------------
# 4. 退还：蒸馏失败 → quota_used-1
# ---------------------------------------------------------------------------
async def test_redistill_does_not_double_charge():
    """重蒸馏同一篇文章**不再重复扣配额**（幂等）。

    背景：这条用例原来叫 test_refund_on_distill_failure，断言
    `quota_consumed is True` 且 `quota_used == 2` —— 也就是要求重蒸时再扣一次。
    那是 CP-DISTILL-DUP 修复之前的行为：那版实跑发现一篇蒸馏被完整跑了两遍
    （相隔 163ms 入队两个 job），本机算力和 LLM 费用双倍、第一遍音频整个丢弃。
    修法是 `already_charged = existed_da is not None or marked`，重蒸直接复用
    原任务行、不再扣费。旧断言等于把那个 bug 钉死成了契约。

    实测：submit 后 quota_used=1，重蒸返回 quota_consumed=False、quota_used 仍为 1。
    """
    uid, token = await new_user(monthly_quota=5)
    r = await submit(token, "https://example.com/refund")
    assert r.status_code == 200, r.text
    article_id = r.json()["article_id"]
    assert await db_quota_used(uid) == 1

    async with client(ai_app, token) as c:
        r = await c.post(
            f"/api/v1/articles/{article_id}/distill", params={"simulate_failure": "true"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "started"
        # 关键断言：已扣过就不��再扣
        assert body["quota_consumed"] is False
        assert body["quota_used"] == 1
    assert await db_quota_used(uid) == 1


# ---------------------------------------------------------------------------
# 4b. 退款覆盖：已移到 tests/ai/test_distill_quota_refund.py
# ---------------------------------------------------------------------------
# 原先这里挂着一条 `test_refund_on_distill_failure_NOT_COVERED`（pytest.skip），
# 记录「退款逻辑没有任何有效测试覆盖」这个缺口 —— 理由是要触发退款必须让蒸馏
# 真跑到 failed，而生产蒸馏走 LangGraph agent + 真实 TTS 队列（单篇约 12 分钟）。
#
# 2026-10-03 补上了：`_refund_quota_once` 的两个依赖（Redis SETNX 幂等锁、
# `quota_service.refund`）都是可替换的，monkeypatch 掉就能直接测，不需要跑真
# 蒸馏。见 backend/tests/ai/test_distill_quota_refund.py（8 个用例，含
# 「Arq 重试 3 次只退 1 次」这条幂等断言、以及 Redis 宕机时仍要退款的降级）。
# 缺口已关闭，这里不再留 skip 占位。


# ---------------------------------------------------------------------------
# 5. 缓存失效：扣减后 GET /quota 拿最新值（不是缓存旧值）
# ---------------------------------------------------------------------------
async def test_cache_invalidated_after_consume():
    uid, token = await new_user(monthly_quota=5)
    before = await get_quota(uid, token)
    assert before["quota_used"] == 0
    assert await cache_service.get_quota(uid) is not None  # GET 后已回填缓存

    r = await submit(token, "https://example.com/invalidate")
    assert r.status_code == 200, r.text

    after = await get_quota(uid, token)
    assert after["quota_used"] == 1  # 不是缓存旧值 0
    assert after["version"] == before["version"] + 1


# ---------------------------------------------------------------------------
# 6. 缓存 miss：删缓存 → GET /quota → 走 DB → 回填
# ---------------------------------------------------------------------------
async def test_cache_miss_fallback_to_db():
    uid, token = await new_user(monthly_quota=5)
    await get_quota(uid, token)  # 先填缓存

    # 模拟缓存过期/丢失（直接删 key，不带版本号栅栏）
    rc = redis.asyncio.Redis(connection_pool=get_redis_pool())
    await rc.delete(cache_service.quota_key(uid))
    await rc.aclose()
    assert await cache_service.get_quota(uid) is None  # 缓存已空

    first = await get_quota(uid, token)
    assert first["cached"] is False  # miss → 查 DB
    assert first["quota_used"] == 0

    second = await get_quota(uid, token)
    assert second["cached"] is True  # 回填命中
    assert second["quota_used"] == 0
