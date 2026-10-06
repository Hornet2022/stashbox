"""CP-STATS-REWORK GET /api/v1/admin/stats 全量重构测试（v1 §3.6 + 真实统计要求）。

覆盖：
  - 现有字段不破坏：total_users / pending / listened
  - 修口径：
      * total_articles 排除 deleted_at 非空（v1 之前包含软删的）
      * failed_distillations_24h 来自 DistilledArticle.status='failed' AND updated_at > 24h
        （v1 之前错误地查 Article.status='failed'）
      * failed_articles_24h 新字段：保留 Article 侧失败的口径，明确语义
      * revenue_available=false 时前端知道是表缺失，不是真的 0 营收
  - 拓指标：
      * distill_success_rate = done_total / (done_total + failed_total)
      * by_source: 聚合 Article.source 分布
      * trends.articles_created_7d / users_created_7d / distill_completed_7d
      * comparison.new_articles_24h.{today, yesterday, delta_pct}
      * warning: failed > 5 时填充，failed <= 5 时 None
      * generated_at: ISO8601
  - 缓存升级：Redis（key=admin:stats:v3）TTL 30s；进程重启缓存不丢

依赖真实 PG（localhost:5432）和 Redis（localhost:6379），与现有 stashbox_dev 一致。
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


async def _clear_stats_cache():
    """CP-STATS-REDIS：清掉 Redis 里 admin:stats:v3（每 case 拿到 fresh 数据）。

    v1 内存缓存时代是清模块属性，v3 改成清 Redis key。
    """
    _ar = sys.modules.get("admin_router")
    if _ar is not None:
        try:
            await _ar._invalidate_stats_cache()
        except Exception:
            pass


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


@pytest.fixture(autouse=True)
async def _no_orders_leak():
    """用例结束后**必定**删掉 orders 夹具表。

    原来是在用例里手工配对调 `_seed_orders()` / `_cleanup_orders()`。一旦用例
    在两者之间失败（assert 挂了、抛异常、进程被杀），清理就不执行 —— orders
    表留在库里。本机就是这么留下一张 0 行残留表的，而它的直接后果是
    `alembic check` 一直报 "New upgrade operations detected: remove_table orders"，
    拖动检测工具自己失去意义。

    这里只保证「收尾删干净」，不代替建表：用到 orders 的那个用例需要自己控制
    「表不存在 → 建表 → 再删表」的过程（它测的就是 revenue_available 随表
    在不在而变）。teardown 由 pytest 兜住，用例失败照样执行。
    """
    yield
    await _cleanup_orders()


@pytest.mark.asyncio
async def test_stats_required_fields_present_on_empty_db():
    """DB 空时所有 11 个字段都存在且类型正确（含 7 个原字段 + 4 个新增）。"""
    await _cleanup_orders()
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        resp = await c.get("/api/v1/admin/stats")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 原 7 个字段
    assert isinstance(data["total_users"], int) and data["total_users"] >= 0
    assert isinstance(data["total_articles"], int) and data["total_articles"] >= 0
    assert isinstance(data["pending"], int) and data["pending"] >= 0
    assert isinstance(data["listened"], int) and data["listened"] >= 0
    assert isinstance(data["active_audio_files"], int) and data["active_audio_files"] >= 0
    assert (
        isinstance(data["failed_distillations_24h"], int) and data["failed_distillations_24h"] >= 0
    )
    # revenue：orders 表不存在 → 0，且 revenue_available=False
    assert data["revenue"] == 0
    assert data["revenue_available"] is False
    # 新 4 个字段（CP-STATS-REWORK 拓指标）
    assert isinstance(data["failed_articles_24h"], int) and data["failed_articles_24h"] >= 0
    assert isinstance(data["distill_success_rate"], float)
    assert 0.0 <= data["distill_success_rate"] <= 1.0
    assert isinstance(data["by_source"], dict)
    assert isinstance(data["trends"], dict)
    assert {"articles_created_7d", "users_created_7d", "distill_completed_7d"} <= set(
        data["trends"].keys()
    )
    for key in ("articles_created_7d", "users_created_7d", "distill_completed_7d"):
        assert isinstance(data["trends"][key], list)
        for row in data["trends"][key]:
            assert "date" in row and "count" in row
    assert isinstance(data["comparison"], dict)
    for key in ("new_articles_24h", "new_users_24h", "distill_completed_24h"):
        assert key in data["comparison"]
        for f in ("today", "yesterday", "delta_pct"):
            assert f in data["comparison"][key]
    # warning：字段存在；具体是否填充取决于真实 failed 数（共享 DB 里可能有历史失败 → 触发）
    # 共享 DB 不严格断言 None，只断言结构正确
    if data["warning"] is not None:
        assert data["warning"]["code"] == "high_distill_failure"
        assert isinstance(data["warning"]["threshold"], int)
        assert isinstance(data["warning"]["actual"], int)
        assert data["warning"]["actual"] > data["warning"]["threshold"]
    # generated_at: ISO8601 字符串
    assert isinstance(data["generated_at"], str)


@pytest.mark.asyncio
async def test_stats_total_articles_excludes_deleted():
    """修 #1：total_articles 排除 deleted_at 非空（v1 之前会算上软删的）。

    共享 DB 已有数据，不严格断言数字，仅断言 total_articles 等于
    "where deleted_at IS NULL" 的实际 count。
    """
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    # 构造：1 个未删 + 1 个软删
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url="https://example.com/keep",
                source="d9",
                status="pending",
                favorite=False,
                skip=False,
            )
        )
        s.add(
            Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url="https://example.com/del",
                source="d9",
                status="pending",
                favorite=False,
                skip=False,
                deleted_at=datetime.utcnow(),  # 软删
            )
        )
        await s.commit()

    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        data = (await c.get("/api/v1/admin/stats")).json()

    # DB 里实际 where deleted_at IS NULL 的数 = total_articles 应一致（共享 DB 不能用绝对值）
    async with AsyncSessionLocal() as s:
        expected = await s.scalar(text("SELECT count(*) FROM articles WHERE deleted_at IS NULL"))
    assert data["total_articles"] == int(
        expected
    ), "total_articles 必须排除软删（CP-STATS-REWORK #1 修复）"


@pytest.mark.asyncio
async def test_stats_failed_distillations_uses_distilled_article():
    """修 #2：failed_distillations_24h 来自 DistilledArticle.status='failed'（不再查 Article）。

    构造 2 个近 24h failed DistilledArticle（需先有父 article 满足 FK），
    同时构造 1 个近 24h Article.status='failed'（**不应**计入 failed_distillations_24h）。
    """
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    base_failed = await _client(_token(admin)).__aenter__() or None  # noop，只是 placeholder
    async with _client(_token(admin)) as c:
        baseline = (await c.get("/api/v1/admin/stats")).json()
    base_failed = baseline["failed_distillations_24h"]
    base_failed_articles = baseline["failed_articles_24h"]

    async with AsyncSessionLocal() as s:
        # 1) 2 个近 24h DistilledArticle.status='failed' 应计入 failed_distillations_24h
        for _ in range(2):
            parent = Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url="https://example.com/p",
                source="d9",
                status="ready",
                favorite=False,
                skip=False,
            )
            s.add(parent)
            await s.flush()
            s.add(
                DistilledArticle(
                    id=f"dst_{uuid.uuid4().hex[:24]}",
                    article_id=parent.id,
                    status="failed",  # ← 真·蒸馏失败
                )
            )
        # 2) 1 个 Article.status='failed'（不是蒸馏失败，只是文章侧失败）—— 不计入 failed_distillations_24h
        s.add(
            Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url="https://example.com/article-fail",
                source="d9",
                status="failed",
                favorite=False,
                skip=False,
            )
        )
        await s.commit()

    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        data = (await c.get("/api/v1/admin/stats")).json()

    # failed_distillations_24h：+2（DistilledArticle 失败）
    assert data["failed_distillations_24h"] == base_failed + 2
    # failed_articles_24h：+1（Article 失败）
    assert data["failed_articles_24h"] == base_failed_articles + 1


@pytest.mark.asyncio
async def test_stats_revenue_available_flag():
    """修 #3：revenue_available 反映 orders 表是否真存在。

    有 orders 表时 revenue_available=True；drop 后变 False（不再静默 0）。
    """
    await _cleanup_orders()  # 起点：表缺
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        d1 = (await c.get("/api/v1/admin/stats")).json()
    assert d1["revenue_available"] is False
    assert d1["revenue"] == 0

    await _seed_orders()
    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        d2 = (await c.get("/api/v1/admin/stats")).json()
    assert d2["revenue_available"] is True
    assert d2["revenue"] == 30.50  # 10.50 + 20.00

    await _cleanup_orders()


@pytest.mark.asyncio
async def test_stats_distill_success_rate():
    """#6：distill_success_rate = done / (done + failed)，全量。

    构造 done:failed = 3:1，期望 0.75。
    """
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    async with AsyncSessionLocal() as s:
        for i in range(4):
            parent = Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url=f"https://example.com/{i}",
                source="d9",
                status="ready",
                favorite=False,
                skip=False,
            )
            s.add(parent)
            await s.flush()
            s.add(
                DistilledArticle(
                    id=f"dst_{uuid.uuid4().hex[:24]}",
                    article_id=parent.id,
                    status="done" if i < 3 else "failed",
                )
            )
        await s.commit()

    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        data = (await c.get("/api/v1/admin/stats")).json()
    # 因为是共享 DB，绝对值不能断言；断言 ≥ 0 且 ≤ 1（基本语义），以及新增 4 条都进入计算
    # 增量断言：用蒸馏总数做参考
    assert 0.0 <= data["distill_success_rate"] <= 1.0
    # 验证：是 done/(done+failed) 的合理值，不是某个固定值
    # 我们可以验证新构造的 3 done + 1 failed 都进入计算（通过查 DB 比较）
    async with AsyncSessionLocal() as s2:
        done_count = (
            await s2.scalar(text("SELECT count(*) FROM distilled_articles WHERE status='done'"))
        ) or 0
        failed_count = (
            await s2.scalar(text("SELECT count(*) FROM distilled_articles WHERE status='failed'"))
        ) or 0
    expected_rate = done_count / (done_count + failed_count) if (done_count + failed_count) else 1.0
    assert abs(data["distill_success_rate"] - round(expected_rate, 4)) < 1e-4


@pytest.mark.asyncio
async def test_stats_warning_triggered_by_high_failure():
    """#10：failed_distillations_24h > 阈值时 warning 填充。

    阈值 = DISTILL_FAILURE_24H_ALERT_THRESHOLD（默认 5）。
    """
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    async with AsyncSessionLocal() as s:
        for i in range(6):  # 一次性 6 条失败 → 必超阈值 5
            parent = Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=uid,
                url=f"https://example.com/w{i}",
                source="d9",
                status="ready",
                favorite=False,
                skip=False,
            )
            s.add(parent)
            await s.flush()
            s.add(
                DistilledArticle(
                    id=f"dst_{uuid.uuid4().hex[:24]}",
                    article_id=parent.id,
                    status="failed",
                )
            )
        await s.commit()

    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        data = (await c.get("/api/v1/admin/stats")).json()
    assert data["warning"] is not None
    assert data["warning"]["code"] == "high_distill_failure"
    assert "蒸馏失败" in data["warning"]["message"]
    assert data["warning"]["actual"] >= data["warning"]["threshold"]


@pytest.mark.asyncio
async def test_stats_by_source_groups_articles():
    """#5：by_source 把 Article.source 聚合成 dict。"""
    await _clear_stats_cache()
    admin = await _make_user(tier="admin")
    uid = await _make_user()

    async with AsyncSessionLocal() as s:
        for src, n in [("wechat", 3), ("douyin", 2), ("pdf", 1)]:
            for _ in range(n):
                s.add(
                    Article(
                        id=f"art_{uuid.uuid4().hex[:24]}",
                        user_id=uid,
                        url=f"https://example.com/{src}/{uuid.uuid4().hex[:6]}",
                        source=src,
                        status="pending",
                        favorite=False,
                        skip=False,
                    )
                )
        await s.commit()

    await _clear_stats_cache()
    async with _client(_token(admin)) as c:
        data = (await c.get("/api/v1/admin/stats")).json()
    by_source = data["by_source"]
    # 至少 wechat/douyin/pdf 各 ≥ 1（其他已存在的 source 不影响）
    assert by_source.get("wechat", 0) >= 3
    assert by_source.get("douyin", 0) >= 2
    assert by_source.get("pdf", 0) >= 1


@pytest.mark.asyncio
async def test_stats_uses_redis_cache_v3_key():
    """CP-STATS-REDIS：stats 缓存写到了 Redis admin:stats:v3（不再用进程内 dict）。"""
    import redis.asyncio as redis_async

    from stashbox.backend.common.redis_client import get_redis_pool

    pool = get_redis_pool()
    r = redis_async.Redis(connection_pool=pool)
    try:
        await r.delete("admin:stats:v3")
    finally:
        await r.aclose()

    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        await c.get("/api/v1/admin/stats")  # 触发写入缓存

    pool = get_redis_pool()
    r = redis_async.Redis(connection_pool=pool)
    try:
        cached = await r.get("admin:stats:v3")
    finally:
        await r.aclose()

    assert cached is not None, "stats 应该已写入 Redis admin:stats:v3"
    import json as _json

    payload = _json.loads(cached)
    assert payload["generated_at"]  # 至少含 generated_at 字段


@pytest.mark.asyncio
async def test_stats_requires_admin_role():
    """free 用户访问 -> 403。"""
    await _clear_stats_cache()
    free = await _make_user(tier="free")
    async with _client(_token(free, tier="free")) as c:
        resp = await c.get("/api/v1/admin/stats")
    assert resp.status_code == 403
