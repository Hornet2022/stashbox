"""B2：ai-service admin 只读端点（A2/A3读/A5/A7）单测。

JSONB 列（tags/raw_content）在 SQLite 编译不了 → 全局 @compiles 补丁渲染成 TEXT，
从而可直接 create_all 相关表做真 session 查询测试。

覆盖 5 端点：health / pool list / evaluations / variants stats / consents，
每端点至少 空库 + 有数据 + 过滤 三类。admin 鉴权用 dependency_overrides 注入。
"""

import importlib.util
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

if not hasattr(JSONB, "_sqlite_patched_b2"):

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "TEXT"

    JSONB._sqlite_patched_b2 = True

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


async def _make_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from stashbox.backend.common.models import (
        ArticleAudioVariant,
        ConsentRecord,
        DistillationEvaluation,
        DistilledArticle,
        FewShotExample,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            DistilledArticle.__table__,
            DistillationEvaluation.__table__,
            FewShotExample.__table__,
            ArticleAudioVariant.__table__,
            ConsentRecord.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _load_ai_app():
    name = "_b2_admin_ai_main"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def client():
    module = _load_ai_app()
    engine, sf = await _make_db()
    from stashbox.backend.common.auth_admin import require_admin_or_operator
    from stashbox.backend.common.database import get_db

    module.app.dependency_overrides[require_admin_or_operator] = lambda: {
        "sub": "999",
        "tier": "admin",
    }

    async def _db():
        async with sf() as s:
            yield s

    module.app.dependency_overrides[get_db] = _db
    ac = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test")
    ac._engine = engine  # type: ignore[attr-defined]
    ac._sf = sf  # type: ignore[attr-defined]
    yield ac
    await ac.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# A2 · health
# ---------------------------------------------------------------------------


async def test_health_empty_pool(client):
    r = await client.get("/api/v1/admin/few-shot-pool/health")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 0
    assert body["health_score"] == 0.0
    assert body["warning"] == "insufficient"  # 空池 < MIN_POOL_SIZE


async def test_health_with_data(client):
    from stashbox.backend.common.models import FewShotExample

    now = datetime.now()
    async with client._sf() as db:
        for i in range(120):
            db.add(
                FewShotExample(
                    id=f"fs_{uuid.uuid4().hex[:24]}",
                    user_id=None,
                    source_pattern=f"pat_{i}",
                    rewrite_text=f"text {i} " + uuid.uuid4().hex,
                    kind="hook",
                    score_avg=4.5 if i % 2 == 0 else 2.0,
                    source_eval_ids="[]",
                    usage_count=1,
                    last_used_at=now,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()
    r = await client.get("/api/v1/admin/few-shot-pool/health")
    body = r.json()
    assert body["total_count"] == 120
    assert body["high_score_count"] == 60
    assert body["low_score_count"] == 60
    assert body["warning"] is None  # 够大 + 无 stale → 无警告


# ---------------------------------------------------------------------------
# A2 · pool list
# ---------------------------------------------------------------------------


async def test_pool_list_filter_kind(client):
    from stashbox.backend.common.models import FewShotExample

    now = datetime.now()
    async with client._sf() as db:
        db.add(
            FewShotExample(
                id=f"fs_h_{uuid.uuid4().hex[:20]}",
                user_id=None,
                source_pattern="p1",
                rewrite_text="hook text",
                kind="hook",
                score_avg=4.2,
                source_eval_ids="[]",
                usage_count=3,
                last_used_at=now,
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            FewShotExample(
                id=f"fs_o_{uuid.uuid4().hex[:20]}",
                user_id=None,
                source_pattern="p2",
                rewrite_text="outro text",
                kind="outro",
                score_avg=3.1,
                source_eval_ids="[]",
                usage_count=0,
                last_used_at=None,
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
    r = await client.get("/api/v1/admin/few-shot-pool?kind=hook")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "hook"
    # min_score 过滤
    r2 = await client.get("/api/v1/admin/few-shot-pool?min_score=4.0")
    assert r2.json()["total"] == 1


# ---------------------------------------------------------------------------
# A3 · evaluations
# ---------------------------------------------------------------------------


async def test_evaluations_list_and_filter(client):
    from stashbox.backend.common.models import DistillationEvaluation

    now = datetime.now()
    async with client._sf() as db:
        for score, flag in ((5, False), (2, False), (4, True)):
            db.add(
                DistillationEvaluation(
                    id=f"eval_{uuid.uuid4().hex[:20]}",
                    task_id=f"dst_{uuid.uuid4().hex[:20]}",
                    user_id=42,
                    hook_score=score,
                    overall_score=score,
                    auto_flag=flag,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()
    r = await client.get("/api/v1/admin/evaluations?user_id=42")
    assert r.json()["total"] == 3
    # auto_flag 过滤
    r2 = await client.get("/api/v1/admin/evaluations?auto_flag=true")
    assert r2.json()["total"] == 1
    # max_score 过滤（低分 = 坏案例）
    r3 = await client.get("/api/v1/admin/evaluations?max_score=2")
    assert r3.json()["total"] == 1
    assert r3.json()["items"][0]["overall_score"] == 2


async def test_evaluations_empty(client):
    r = await client.get("/api/v1/admin/evaluations")
    assert r.status_code == 200
    assert r.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}


# ---------------------------------------------------------------------------
# A5 · variants stats
# ---------------------------------------------------------------------------


async def test_variants_stats_coverage(client):
    from stashbox.backend.common.models import (
        ArticleAudioVariant,
        DistilledArticle,
    )

    now = datetime.now()
    async with client._sf() as db:
        for i in range(4):
            db.add(
                DistilledArticle(
                    id=f"dst_{uuid.uuid4().hex[:20]}",
                    article_id=f"art_{uuid.uuid4().hex[:20]}",
                    status="done",
                    audio_url=f"https://x/{i}.m4a",
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.flush()
        # 只给 1 篇建 96k + 64k 变体 → coverage = 1/4
        one = (await db.execute(select(DistilledArticle))).scalars().first()
        for bitrate, size in ((96, 700000), (64, 480000)):
            db.add(
                ArticleAudioVariant(
                    id=f"avar_{uuid.uuid4().hex[:20]}",
                    distilled_article_id=one.id,
                    bitrate=bitrate,
                    file_size_bytes=size,
                    oss_key=f"audio/x.{bitrate}k.m4a",
                    format="m4a",
                    duration_sec=300,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()
    r = await client.get("/api/v1/admin/audio-variants/stats")
    body = r.json()
    assert body["done_articles"] == 4
    assert body["covered_articles"] == 1
    assert body["coverage_ratio"] == 0.25
    by = {x["bitrate"]: x for x in body["by_bitrate"]}
    assert by[96]["count"] == 1
    assert by[64]["count"] == 1


async def test_variants_stats_no_data(client):
    r = await client.get("/api/v1/admin/audio-variants/stats")
    body = r.json()
    assert body["by_bitrate"] == []
    assert body["coverage_ratio"] == 0.0


# ---------------------------------------------------------------------------
# A7 · consents
# ---------------------------------------------------------------------------


async def test_consents_list_and_filter(client):
    from stashbox.backend.common.models import ConsentRecord

    now = datetime.now()
    async with client._sf() as db:
        for uid, pen in ((10, True), (11, False), (12, True)):
            db.add(
                ConsentRecord(
                    user_id=uid,
                    personalization_enabled=pen,
                    cross_user_share_enabled=False,
                    consent_at=now.isoformat(),
                    consent_version="v2",
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()
    r = await client.get("/api/v1/admin/consents")
    assert r.json()["total"] == 3
    r2 = await client.get("/api/v1/admin/consents?personalization_enabled=true")
    assert r2.json()["total"] == 2
    # 无敏感自由文本字段
    item = r.json()["items"][0]
    assert "comment" not in item
    assert set(item) == {
        "user_id",
        "personalization_enabled",
        "cross_user_share_enabled",
        "consent_version",
        "consent_at",
        "created_at",
    }


# ---------------------------------------------------------------------------
# 鉴权 + 路由注册
# ---------------------------------------------------------------------------


async def test_admin_endpoints_protected_by_auth():
    """admin 路由函数必须挂 require_admin_or_operator 依赖（避免忘加鉴权裸奔）。

    轻量校验路由签名，不发请求 —— 绕开 app 单例 override 污染问题。
    """
    import admin_router

    protected = 0
    for route in admin_router.router.routes:
        dep_names = {d.call.__name__ for d in route.dependant.dependencies}
        assert "require_admin_or_operator" in dep_names, f"{route.path} 缺 admin 鉴权依赖"
        protected += 1
    assert protected == 6  # health/list/evaluations/stats/consents/ab-report（B4 新增）


async def test_b2_routes_registered():
    sys.path.insert(0, str(BACKEND_DIR / "api-gateway"))
    try:
        import importlib

        importlib.invalidate_caches()
        cfg = importlib.import_module("config")
        get_paths = {r.path for r in cfg.ROUTES if r.method == "GET"}
        for p in (
            "/api/v1/admin/few-shot-pool/health",
            "/api/v1/admin/few-shot-pool",
            "/api/v1/admin/evaluations",
            "/api/v1/admin/audio-variants/stats",
            "/api/v1/admin/consents",
        ):
            assert p in get_paths, f"{p} 未注册 ROUTES"
    finally:
        sys.path.remove(str(BACKEND_DIR / "api-gateway"))
