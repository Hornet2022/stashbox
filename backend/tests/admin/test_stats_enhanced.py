"""CP3.6-A3 GET /api/v1/admin/stats 增强字段单测（v1 §3.6）。

覆盖：
  - 现有字段（total_users / total_articles / pending / listened）不破坏
  - 新增字段：revenue / active_audio_files / failed_distillations_24h
      * 有值：构造对应数据后字段 > 0
      * 无值（DB 空）：字段存在且为 0（含 orders 表缺失时 revenue=0）

依赖真实 PG（/tmp:5432）。orders 表在仓库无独立 migration，revenue 查询缺表时返回 0；
本测试在"有值"用例用 raw SQL 临时建 orders 表并插入本月已支付订单，验证求和逻辑。
"""
import importlib.util
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import (
    Article,
    DistilledArticle,
    User,
)
from stashbox.backend.common.models.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


content_app = _load_app("_cp36a3_stats_main", "content-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, Article.__table__, DistilledArticle.__table__],
        )
    yield


async def _make_user(tier: str = "free") -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp36a3s_" + uuid.uuid4().hex[:24],
            nickname="u_" + uuid.uuid4().hex[:6],
            tier=tier,
            monthly_quota=5,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


def _token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _seed_orders():
    """临时建 orders 表（仓库无独立 migration）并插入本月一笔已支付 + 一笔未支付。"""
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS orders ("
                "  id SERIAL PRIMARY KEY,"
                "  amount NUMERIC(10,2) NOT NULL,"
                "  status TEXT NOT NULL,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                ")"
            )
        )
        await s.execute(text("DELETE FROM orders"))
        await s.execute(
            text(
                "INSERT INTO orders (amount, status, created_at) VALUES "
                "(:a1, 'paid', NOW()), (:a2, 'paid', NOW()), (:a3, 'refunded', NOW())"
            ),
            {"a1": 10.50, "a2": 20.00, "a3": 99.00},
        )
        await s.commit()


async def _cleanup_orders():
    async with AsyncSessionLocal() as s:
        await s.execute(text("DROP TABLE IF EXISTS orders"))
        await s.commit()


@pytest.mark.asyncio
async def test_stats_fields_present_on_empty_db():
    """DB 无本轮数据时：新增字段存在、类型正确、非负；orders 表缺失时 revenue=0。"""
    await _cleanup_orders()  # 清掉可能残留的 orders 表，确保 revenue 走缺表分支
    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        resp = await c.get("/api/v1/admin/stats")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 现有字段不破坏
    assert "total_users" in data
    assert "total_articles" in data
    assert "pending" in data
    assert "listened" in data
    # 新增字段存在且为非负整数（共享 DB 可能已有历史数据，故不严格 == 0）
    assert isinstance(data["active_audio_files"], int) and data["active_audio_files"] >= 0
    assert isinstance(data["failed_distillations_24h"], int) and data["failed_distillations_24h"] >= 0
    # revenue：orders 表不存在 → 0
    assert data["revenue"] == 0


@pytest.mark.asyncio
async def test_stats_fields_with_values():
    """有值：构造数据后新增字段相对基线有正确增量。"""
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    # 先取基线（共享 DB 可能已有历史数据），用增量断言避免串扰
    async with _client(_token(admin)) as c:
        base = (await c.get("/api/v1/admin/stats")).json()
    base_failed = base["failed_distillations_24h"]
    base_audio = base["active_audio_files"]

    # failed_distillations_24h：近 24h 内 failed 文章
    async with AsyncSessionLocal() as s:
        for _ in range(2):
            a = Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url="https://example.com/f",
                source="d9",
                status="failed",
                favorite=False,
                skip=False,
            )
            s.add(a)
        # 过期失败文章（>24h）不应计入
        old = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=uid,
            url="https://example.com/old",
            source="d9",
            status="failed",
            favorite=False,
            skip=False,
            created_at=datetime(2020, 1, 1),  # naive，匹配 TIMESTAMP 列
        )
        s.add(old)
        # active_audio_files：done 且有 audio_url（需先建父 article 满足 FK）
        pa1 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}", user_id=uid,
            url="https://example.com/pa1", source="d9",
            status="ready", favorite=False, skip=False,
        )
        pa2 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}", user_id=uid,
            url="https://example.com/pa2", source="d9",
            status="ready", favorite=False, skip=False,
        )
        s.add_all([pa1, pa2])
        await s.flush()
        d1 = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=pa1.id,
            status="done",
            audio_url="https://oss/a.m4a",
        )
        d2 = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=pa2.id,
            status="done",
            audio_url=None,  # 无 audio_url 不计入
        )
        s.add(d1)
        s.add(d2)
        await s.commit()

    await _seed_orders()
    try:
        async with _client(_token(admin)) as c:
            resp = await c.get("/api/v1/admin/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # 仅新增的 2 条近 24h failed 计入（过期那条不算）
        assert data["failed_distillations_24h"] == base_failed + 2
        # 仅 d1（done + 有 audio_url）计入，d2 无 url 不算
        assert data["active_audio_files"] == base_audio + 1
        # revenue = 10.50 + 20.00 = 30.50（refunded 不算）
        assert data["revenue"] == 30.50
    finally:
        await _cleanup_orders()


@pytest.mark.asyncio
async def test_stats_requires_admin_role():
    """free 用户访问 -> 403。"""
    free = await _make_user(tier="free")
    async with _client(_token(free, tier="free")) as c:
        resp = await c.get("/api/v1/admin/stats")
    assert resp.status_code == 403
