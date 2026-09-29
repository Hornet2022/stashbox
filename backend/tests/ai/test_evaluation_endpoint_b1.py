"""B1 / G1：4 维评分提交（evaluation_service + ai-service 端点）单测。

覆盖：
- validate_score / validate_evaluation_payload: 5 cases
- submit_user_evaluation（SQLite 真 session，联动入池+画像）: 4 cases
- 端点（ASGITransport + dependency_overrides，无 PG）: 5 cases
"""

import importlib.util
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_db():
    """SQLite 内存库：evaluations + few_shot + patterns（无 JSONB，可直接建表）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from stashbox.backend.common.models import (
        DistillationEvaluation,
        FewShotExample,
        UserListeningPattern,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            DistillationEvaluation.__table__,
            FewShotExample.__table__,
            UserListeningPattern.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _da(script_text="开场钩子段\n\n正文段\n\n结尾段"):
    from stashbox.backend.common.models import DistilledArticle

    return DistilledArticle(
        id=f"dst_{uuid.uuid4().hex[:24]}",
        article_id=f"art_{uuid.uuid4().hex[:24]}",
        status="done",
        audio_url="https://mem.example/audio/x.m4a",
        duration_sec=300,
        script_text=script_text,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# 1. 校验纯函数
# ---------------------------------------------------------------------------


def test_validate_score_ok():
    from distill.evaluation_service import validate_score

    validate_score(4, "overall_score", required=True)
    validate_score(None, "hook_score")  # 可选 None 放行


def test_validate_score_out_of_range():
    from distill.evaluation_service import validate_score
    from stashbox.backend.common.exceptions import InvalidRequest

    for bad in (0, 6, -1):
        with pytest.raises(InvalidRequest):
            validate_score(bad, "overall_score", required=True)


def test_validate_score_bool_rejected():
    """True 是 int 子类但语义错误 → 拒。"""
    from distill.evaluation_service import validate_score
    from stashbox.backend.common.exceptions import InvalidRequest

    with pytest.raises(InvalidRequest):
        validate_score(True, "overall_score", required=True)


def test_validate_payload_required_overall():
    from distill.evaluation_service import validate_evaluation_payload
    from stashbox.backend.common.exceptions import InvalidRequest

    class _Req:
        hook_score = None
        section_score = None
        outro_score = None
        rhythm_score = None
        overall_score = None
        comment = None
        skip_reason = None

    with pytest.raises(InvalidRequest, match="overall_score is required"):
        validate_evaluation_payload(_Req())


def test_validate_payload_skip_reason_length():
    from distill.evaluation_service import validate_evaluation_payload
    from stashbox.backend.common.exceptions import InvalidRequest

    class _Req:
        hook_score = 4
        section_score = None
        outro_score = None
        rhythm_score = None
        overall_score = 4
        comment = None
        skip_reason = "x" * 33

    with pytest.raises(InvalidRequest, match="skip_reason too long"):
        validate_evaluation_payload(_Req())


# ---------------------------------------------------------------------------
# 2. submit_user_evaluation（真联动）
# ---------------------------------------------------------------------------


async def test_submit_score4_pools_and_updates_pattern():
    """overall=4 + 有 hook 文本 → 入池 true + 画像行创建（首条即建，avg NULL 冷启动保护内置）。"""
    from distill.evaluation_service import submit_user_evaluation

    engine, sf = await _make_db()
    async with sf() as db:
        da = _da()
        result = await submit_user_evaluation(db, da, user_id=101, overall_score=4, hook_score=5)
        await db.commit()
        assert result["in_few_shot_pool"] is True
        assert result["pattern_updated"] is True
        assert result["id"].startswith("eval_")

        from sqlalchemy import func, select

        from stashbox.backend.common.models import (
            DistillationEvaluation,
            FewShotExample,
            UserListeningPattern,
        )

        ev = await db.scalar(
            select(DistillationEvaluation).where(DistillationEvaluation.id == result["id"])
        )
        assert ev.auto_flag is False
        assert ev.task_id == da.id
        assert ev.hook_score == 5
        assert await db.scalar(select(func.count()).select_from(FewShotExample)) == 1
        pat = await db.scalar(
            select(UserListeningPattern).where(UserListeningPattern.user_id == 101)
        )
        assert pat is not None
    await engine.dispose()


async def test_submit_score2_no_pool():
    """overall=2 → 池门槛内置拒绝，但画像仍更新（联动 1 False / 联动 2 True）。"""
    from distill.evaluation_service import submit_user_evaluation

    engine, sf = await _make_db()
    async with sf() as db:
        result = await submit_user_evaluation(
            db, _da(), user_id=102, overall_score=2, comment="太赶了"
        )
        await db.commit()
        assert result["in_few_shot_pool"] is False
        assert result["pattern_updated"] is True
    await engine.dispose()


async def test_submit_no_script_text_no_pool():
    """script_text 为空 → 无源文本，池联动跳过（False），画像正常。"""
    from distill.evaluation_service import submit_user_evaluation

    engine, sf = await _make_db()
    async with sf() as db:
        da = _da(script_text=None)
        result = await submit_user_evaluation(db, da, user_id=103, overall_score=5)
        await db.commit()
        assert result["in_few_shot_pool"] is False
        assert result["pattern_updated"] is True
    await engine.dispose()


async def test_submit_link_failure_keeps_evaluation():
    """池联动内部炸（monkeypatch）→ 只降级标志，评分行仍写入。"""
    import distill.evaluation_service as svc
    from distill.evaluation_service import submit_user_evaluation

    engine, sf = await _make_db()
    orig = svc.add_high_score_to_pool

    async def _boom(*a, **k):
        raise RuntimeError("simulated pool failure")

    svc.add_high_score_to_pool = _boom
    try:
        async with sf() as db:
            result = await submit_user_evaluation(db, _da(), user_id=104, overall_score=5)
            await db.commit()
            assert result["in_few_shot_pool"] is False
            from sqlalchemy import select

            from stashbox.backend.common.models import DistillationEvaluation

            ev = await db.scalar(
                select(DistillationEvaluation).where(DistillationEvaluation.id == result["id"])
            )
            assert ev is not None  # 评分没被联动失败吞掉
    finally:
        svc.add_high_score_to_pool = orig
    await engine.dispose()


# ---------------------------------------------------------------------------
# 3. 端点（ASGITransport + overrides，无 PG）
# ---------------------------------------------------------------------------


def _load_ai_app():
    """ai-service 目录带连字符 → 按文件加载（仿 tests/admin 先例）。"""
    name = "_b1_eval_ai_main"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ai_client():
    """app + 依赖替身：require_user → uid 7；get_db → 抛错占位（各测试自行 override）。"""
    module = _load_ai_app()
    return module


async def _client_with_db(module, sf, tier_uid=7):
    from stashbox.backend.common.auth import require_user
    from stashbox.backend.common.database import get_db

    module.app.dependency_overrides[require_user] = lambda: {"sub": str(tier_uid)}

    async def _db():
        async with sf() as s:
            yield s

    module.app.dependency_overrides[get_db] = _db
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test")


async def test_endpoint_validation_400(ai_client):
    """overall 越界 → 业务 400（不进 DB 查询）。"""
    module = ai_client
    engine, sf = await _make_db()

    def _boom(*a, **k):
        raise AssertionError("归属校验不应在校验失败后执行")

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _boom
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.post(
                "/api/v1/distill/dst_x/evaluation",
                json={"overall_score": 9},
            )
        assert r.status_code == 400
        assert r.json()["code"] == 4001
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_endpoint_not_found(ai_client):
    module = ai_client
    from stashbox.backend.common.exceptions import NotFound

    engine, sf = await _make_db()

    async def _none(*a, **k):
        raise NotFound(message="task not found")

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _none
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.post(
                "/api/v1/distill/dst_missing/evaluation",
                json={"overall_score": 4},
            )
        assert r.status_code == 404
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_endpoint_forbidden(ai_client):
    module = ai_client
    from stashbox.backend.common.exceptions import Forbidden

    engine, sf = await _make_db()

    async def _forb(*a, **k):
        raise Forbidden(message="not the owner")

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _forb
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.post("/api/v1/distill/dst_x/evaluation", json={"overall_score": 4})
        assert r.status_code == 403
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_endpoint_success_end_to_end(ai_client):
    """完整链路：校验 → 归属（stub da）→ 写表 + 联动 → 200 + 落库可查。"""
    module = ai_client
    engine, sf = await _make_db()
    da = _da()

    async def _own(*a, **k):
        return da

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _own
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.post(
                f"/api/v1/distill/{da.id}/evaluation",
                json={
                    "hook_score": 4,
                    "section_score": 5,
                    "outro_score": None,
                    "rhythm_score": 4,
                    "overall_score": 4,
                    "comment": "结构清晰",
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == da.id
        assert body["in_few_shot_pool"] is True
        assert body["pattern_updated"] is True
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_gateway_routes_registered():
    """G5：3 条 distill 系路由已显式注册进 ROUTES。"""
    sys.path.insert(0, str(BACKEND_DIR / "api-gateway"))
    try:
        import importlib

        importlib.invalidate_caches()
        cfg = importlib.import_module("config")
        paths = {(r.method, r.path) for r in cfg.ROUTES}
        assert ("GET", "/api/v1/distill/{task_id}/variants") in paths
        assert ("POST", "/api/v1/distill/{task_id}/variants/{bitrate}/warm") in paths
        assert ("POST", "/api/v1/distill/{task_id}/evaluation") in paths
        assert ("GET", "/api/v1/distill/{task_id}/evaluation") in paths
    finally:
        sys.path.remove(str(BACKEND_DIR / "api-gateway"))


# ---------------------------------------------------------------------------
# 4. 读回端点 GET .../evaluation（评分闭环读侧）
# ---------------------------------------------------------------------------


async def test_get_evaluation_returns_latest_own(ai_client):
    """POST 过两条 → GET 只回**自己**最新那条，且带 created_at。"""
    module = ai_client
    from stashbox.backend.common.models import DistillationEvaluation

    engine, sf = await _make_db()
    da = _da()

    async def _own(*a, **k):
        return da

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _own
    try:
        async with sf() as db:
            db.add_all(
                [
                    DistillationEvaluation(
                        id="eval_old",
                        task_id=da.id,
                        user_id=7,
                        overall_score=2,
                        auto_flag=False,
                        created_at=datetime(2026, 1, 1),
                        updated_at=datetime(2026, 1, 1),
                    ),
                    DistillationEvaluation(
                        id="eval_new",
                        task_id=da.id,
                        user_id=7,
                        hook_score=5,
                        overall_score=5,
                        comment="改完更好",
                        auto_flag=False,
                        created_at=datetime(2026, 9, 1),
                        updated_at=datetime(2026, 9, 1),
                    ),
                ]
            )
            await db.commit()

        client = await _client_with_db(module, sf)
        async with client:
            r = await client.get(f"/api/v1/distill/{da.id}/evaluation")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "eval_new"  # 最新那条，不是第一条
        assert body["overall_score"] == 5
        assert body["hook_score"] == 5
        assert body["comment"] == "改完更好"
        assert body["created_at"] is not None
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_get_evaluation_empty_returns_200_null(ai_client):
    """没评过 → 200 + 全 null，**不是 404**（调用方要区分"没评"和"没这篇"）。"""
    module = ai_client
    engine, sf = await _make_db()
    da = _da()

    async def _own(*a, **k):
        return da

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _own
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.get(f"/api/v1/distill/{da.id}/evaluation")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] is None
        assert body["overall_score"] is None
        assert body["task_id"] == da.id  # 仍回显 task_id，客户端好判"这篇存在但没评"
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_get_evaluation_excludes_other_users_and_auto(ai_client):
    """别人的评分 + auto_flag=true 的自动评分都不该出现在"我的评分"里。"""
    module = ai_client
    from stashbox.backend.common.models import DistillationEvaluation

    engine, sf = await _make_db()
    da = _da()

    async def _own(*a, **k):
        return da

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _own
    try:
        async with sf() as db:
            db.add_all(
                [
                    DistillationEvaluation(
                        id="eval_other_user",
                        task_id=da.id,
                        user_id=999,
                        overall_score=1,
                        auto_flag=False,
                        created_at=datetime(2026, 9, 2),
                        updated_at=datetime(2026, 9, 2),
                    ),
                    DistillationEvaluation(
                        id="eval_auto",
                        task_id=da.id,
                        user_id=7,
                        overall_score=4,
                        auto_flag=True,
                        created_at=datetime(2026, 9, 3),
                        updated_at=datetime(2026, 9, 3),
                    ),
                ]
            )
            await db.commit()

        client = await _client_with_db(module, sf)
        async with client:
            r = await client.get(f"/api/v1/distill/{da.id}/evaluation")
        assert r.status_code == 200
        # 两条都不该被返回 → 视为"没评过"
        assert r.json()["id"] is None
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()


async def test_get_evaluation_forbidden_propagates(ai_client):
    """非 owner → 403 透传（不能因为"没评过"就 200，那会泄露文章存在性）。"""
    module = ai_client
    from stashbox.backend.common.exceptions import Forbidden

    engine, sf = await _make_db()

    async def _forb(*a, **k):
        raise Forbidden(message="not the owner")

    orig = module._get_owned_distilled_article
    module._get_owned_distilled_article = _forb
    try:
        client = await _client_with_db(module, sf)
        async with client:
            r = await client.get("/api/v1/distill/dst_other/evaluation")
        assert r.status_code == 403
    finally:
        module._get_owned_distilled_article = orig
        await engine.dispose()
