"""B4：A/B 数据底座测试（0029 列 + ab_group 落库 + ab-report 端点）。

覆盖：
1. FewShotSelectorHook：ab_group 按 user_id % 100 < 30 分桶（含冷启动路径也分桶）
2. _write_final_to_db：ab_group / is_personalized 写进 distilled_articles
3. GET /api/v1/admin/ab-report：四指标聚合 + pre_experiment 兜底 + 鉴权依赖
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

if not hasattr(JSONB, "_sqlite_patched_b4"):

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite_b4(type_, compiler, **kw):  # noqa: ANN001
        return "TEXT"

    JSONB._sqlite_patched_b4 = True

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


async def _make_db_b4():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from stashbox.backend.common.models import (
        Article,
        DistillationEvaluation,
        DistilledArticle,
        Feedback,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            Article.__table__,
            DistilledArticle.__table__,
            DistillationEvaluation.__table__,
            Feedback.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 1. FewShotSelectorHook 分桶
# ---------------------------------------------------------------------------


def _ctx(user_id: int, feedback_count: int = 0):
    from distill.schemas import DistillContext, UserListeningPattern

    profile = None
    if feedback_count:
        profile = UserListeningPattern(
            user_id=user_id,
            feedback_count=feedback_count,
            last_updated=datetime.now(),
        )
    return DistillContext(
        task_id=f"dst_{uuid.uuid4().hex[:24]}",
        article_id=f"art_{uuid.uuid4().hex[:24]}",
        user_id=user_id,
        url="https://example.com/a",
        raw_content="x",
        user_profile=profile,
    )


class _RealLikeSession:
    """骗过 _is_real_session 检测（有 in_transaction 属性）；查询路径让它抛错走 except 分支。"""

    in_transaction = True

    async def execute(self, *a, **k):
        raise RuntimeError("no selector db in this test")


async def test_ab_group_bucketing_values():
    """直接验证分桶函数语义：user_id % 100 < 30 → personalized。"""
    from distill.hooks_impl import FewShotSelectorHook

    hook = FewShotSelectorHook()

    # 冷启动（无 profile）：仍分桶，但 few-shot 不启用（is_personalized False）
    ctx_cold = _ctx(user_id=105)  # 105 % 100 = 5 < 30 → personalized
    await hook(ctx_cold, object(), _RealLikeSession())
    assert ctx_cold.ab_group == "personalized"
    assert ctx_cold.is_personalized is False

    ctx_cold2 = _ctx(user_id=150)  # 50 ≥ 30 → general
    await hook(ctx_cold2, object(), _RealLikeSession())
    assert ctx_cold2.ab_group == "general"

    # 有画像但 selector 内部抛错（异常路径）：分桶仍已写入（在 try 之前）
    ctx = _ctx(user_id=42, feedback_count=10)  # 42 % 100 = 42 → general
    await hook(ctx, object(), _RealLikeSession())
    assert ctx.ab_group == "general"
    assert ctx.is_personalized is False


# ---------------------------------------------------------------------------
# 2. _write_final_to_db 落库
# ---------------------------------------------------------------------------


async def test_write_final_to_db_persists_ab_columns():
    from distill.pipeline import DistillPipeline
    from distill.schemas import AudioConcatOutput, DistillContext
    from stashbox.backend.common.models import DistilledArticle

    engine, sf = await _make_db_b4()
    try:
        task_id = f"dst_{uuid.uuid4().hex[:24]}"
        art_id = f"art_{uuid.uuid4().hex[:24]}"
        async with sf() as db:
            db.add(DistilledArticle(id=task_id, article_id=art_id, status="step4_concatenating"))
            await db.commit()

        ctx = DistillContext(
            task_id=task_id,
            article_id=art_id,
            user_id=3,
            url="https://example.com",
            raw_content="x",
        )
        ctx.final = AudioConcatOutput(audio_url="https://cdn/x.m4a", duration_sec=300)
        ctx.ab_group = "personalized"
        ctx.is_personalized = True

        pipeline = DistillPipeline(llm=None, db_session_factory=sf)
        await pipeline._write_final_to_db(ctx)

        async with sf() as db:
            row = await db.scalar(select(DistilledArticle).where(DistilledArticle.id == task_id))
        assert row.ab_group == "personalized"
        assert row.is_personalized is True
        assert row.audio_url == "https://cdn/x.m4a"
    finally:
        await engine.dispose()


async def test_write_final_to_db_null_for_pre_experiment():
    """未设 ab_group（历史路径/测试直接 run）→ 落 NULL。"""
    from distill.pipeline import DistillPipeline
    from distill.schemas import AudioConcatOutput, DistillContext
    from stashbox.backend.common.models import DistilledArticle

    engine, sf = await _make_db_b4()
    try:
        task_id = f"dst_{uuid.uuid4().hex[:24]}"
        art_id = f"art_{uuid.uuid4().hex[:24]}"
        async with sf() as db:
            db.add(DistilledArticle(id=task_id, article_id=art_id, status="running"))
            await db.commit()

        ctx = DistillContext(
            task_id=task_id,
            article_id=art_id,
            user_id=9,
            url="https://example.com",
            raw_content="x",
        )
        ctx.final = AudioConcatOutput(audio_url="", duration_sec=0)
        pipeline = DistillPipeline(llm=None, db_session_factory=sf)
        await pipeline._write_final_to_db(ctx)

        async with sf() as db:
            row = await db.scalar(select(DistilledArticle).where(DistilledArticle.id == task_id))
        assert row.ab_group is None
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. compute_ab_report 服务层
# ---------------------------------------------------------------------------


async def _seed_report_data(sf):
    """两组各 1 篇 done 任务 + pre_experiment 1 篇；播放/完听/评分/跳过事件布点。"""
    from stashbox.backend.common.models import (
        Article,
        DistillationEvaluation,
        DistilledArticle,
        Feedback,
    )

    now = datetime.now()
    fb_id = 1  # SQLite 不自动为 BigInteger 主键生成 id，显式赋值
    async with sf() as db:
        for gid, group, uid in (
            ("p", "personalized", 101),
            ("g", "general", 202),
            ("n", None, 303),
        ):
            art = Article(
                id=f"art_{gid}",
                user_id=uid,
                url=f"https://example.com/{gid}",
                skip=(gid == "g"),
            )
            da = DistilledArticle(
                id=f"dst_{gid}",
                article_id=art.id,
                status="done",
                audio_url=f"https://cdn/{gid}.m4a",
                ab_group=group,
                is_personalized=(group == "personalized"),
            )
            db.add_all([art, da])
            # 播放事件：p=2 次（复听）+1 complete；g=1 次；n=1 次
            plays = {"p": 2, "g": 1, "n": 1}[gid]
            for i in range(plays):
                db.add(
                    Feedback(
                        id=fb_id,
                        user_id=uid,
                        article_id=art.id,
                        type="audio_play_start",
                        created_at=now,
                    )
                )
                fb_id += 1
            if gid == "p":
                db.add(
                    Feedback(
                        id=fb_id,
                        user_id=uid,
                        article_id=art.id,
                        type="audio_complete",
                        created_at=now,
                    )
                )
                fb_id += 1
            # 评分：p=5, g=3（pre 组无评分）
            if gid != "n":
                db.add(
                    DistillationEvaluation(
                        id=f"eval_{gid}",
                        task_id=da.id,
                        user_id=uid,
                        overall_score=5 if gid == "p" else 3,
                        created_at=now,
                        updated_at=now,
                    )
                )
        await db.commit()


async def test_compute_ab_report_metrics():
    from distill.ab_report import compute_ab_report

    engine, sf = await _make_db_b4()
    try:
        await _seed_report_data(sf)
        async with sf() as db:
            report = await compute_ab_report(db)
        by_group = {g["group"]: g for g in report["groups"]}
        assert set(by_group) == {"personalized", "general", "pre_experiment"}

        p = by_group["personalized"]
        assert p["tasks"] == 1
        assert p["avg_overall_score"] == 5.0
        assert p["eval_count"] == 1
        assert p["play_count"] == 2
        assert p["complete_count"] == 1
        assert p["completion_rate"] == 0.5
        assert p["play_pairs"] == 1
        assert p["rewatch_pairs"] == 1
        assert p["rewatch_rate"] == 1.0
        assert p["skip_rate"] == 0.0

        g = by_group["general"]
        assert g["avg_overall_score"] == 3.0
        assert g["completion_rate"] == 0.0
        assert g["rewatch_rate"] == 0.0
        assert g["skip_count"] == 1
        assert g["skip_rate"] == 1.0

        n = by_group["pre_experiment"]
        assert n["eval_count"] == 0
        assert n["avg_overall_score"] is None
        assert n["play_pairs"] == 1
        assert "pre_experiment" in report["caveats"][0]
    finally:
        await engine.dispose()


async def test_compute_ab_report_date_filter():
    from datetime import timedelta

    from distill.ab_report import compute_ab_report

    engine, sf = await _make_db_b4()
    try:
        await _seed_report_data(sf)
        async with sf() as db:
            # created_at 默认 now() → 未来区间应查空
            future = datetime.now() + timedelta(days=1)
            report = await compute_ab_report(db, date_from=future)
        assert report["groups"] == []
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. GET /admin/ab-report 端点
# ---------------------------------------------------------------------------


def _load_ai_app_b4():
    name = "_b4_admin_ai_main"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_ai_admin_router():
    """按文件路径加载 **ai-service** 的 admin_router。

    不能写 `import admin_router`：content-service 也有同名 admin_router.py，
    两边都是顶层 import，谁先加载谁占住 sys.modules。同时跑 tests/ai +
    tests/gateway 时 gateway 的 conftest 会先加载 content-service，于是裸
    import 拿到 content-service 那个（没有 ab-report 路由）。仓库里
    test_admin_tts_test_classifier.py 早就记了这个问题，这里沿用同一做法。
    """
    name = "_b4_ai_admin_router"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "admin_router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def b4_client():
    module = _load_ai_app_b4()
    engine, sf = await _make_db_b4()
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


async def test_ab_report_endpoint(b4_client):
    await _seed_report_data(b4_client._sf)
    r = await b4_client.get("/api/v1/admin/ab-report")
    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 3
    assert body["groups"][0]["group"] == "personalized"  # 排序：personalized 在前
    assert len(body["caveats"]) == 2


async def test_ab_report_endpoint_empty_db(b4_client):
    r = await b4_client.get("/api/v1/admin/ab-report")
    assert r.status_code == 200
    assert r.json()["groups"] == []


async def test_ab_report_endpoint_requires_admin():
    """路由签名必须挂 require_admin_or_operator（对齐 B2 鉴权口径）。"""
    ar_mod = _load_ai_admin_router()

    from stashbox.backend.common.auth_admin import require_admin_or_operator

    target = [
        rt for rt in ar_mod.router.routes if getattr(rt, "path", "") == "/api/v1/admin/ab-report"
    ]
    assert len(target) == 1
    dep_names = []

    def _walk(d):
        dep_names.append(d.call)
        for sub in d.dependencies:
            _walk(sub)

    _walk(target[0].dependant)
    assert require_admin_or_operator in dep_names


# ---------------------------------------------------------------------------
# 5. ORM 新列存在性（迁移 0029 与模型对齐）
# ---------------------------------------------------------------------------


def test_model_columns_match_migration():
    from stashbox.backend.common.models import DistillationEvaluation, DistilledArticle

    da_cols = {c.name for c in DistilledArticle.__table__.columns}
    assert {"ab_group", "is_personalized"} <= da_cols

    ev_cols = {c.name for c in DistillationEvaluation.__table__.columns}
    assert "evaluator_id" in ev_cols
    fk_cols = {fk.column.name for fk in DistillationEvaluation.__table__.foreign_keys} | {
        fk.parent.name for fk in DistillationEvaluation.__table__.foreign_keys
    }
    assert "evaluator_id" in fk_cols


def test_migration_0029_chain():
    """0029 迁移存在且 down_revision=0028，列名与模型一致。"""
    import importlib.util as iu

    mig_path = BACKEND_DIR / "alembic" / "versions" / "0029_ab_eval_attribution.py"
    spec = iu.spec_from_file_location("_mig0029", mig_path)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0029"
    assert mod.down_revision == "0028"
