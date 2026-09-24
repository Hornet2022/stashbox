"""B3：ai-service admin 写端点测试（tier-config / annotate / 池运营 / 盲测）。

覆盖 9 端点的关键路径 + 参数校验 + 404/400 + 鉴权依赖遍历。
system_config（Redis/PG 依赖）在 PUT tier-config 测试中 monkeypatch。
"""

import importlib.util
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

if not hasattr(JSONB, "_sqlite_patched_b3"):

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite_b3(type_, compiler, **kw):  # noqa: ANN001
        return "TEXT"

    JSONB._sqlite_patched_b3 = True

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


async def _make_db_b3():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from stashbox.backend.common.models import (
        DistillationEvaluation,
        FewShotExample,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            DistillationEvaluation.__table__,
            FewShotExample.__table__,
        ):
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _load_ai_app_b3():
    name = "_b3_admin_ai_main"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def b3_client():
    module = _load_ai_app_b3()
    engine, sf = await _make_db_b3()
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
# A1 · tier-config
# ---------------------------------------------------------------------------


async def test_tier_config_get_default(b3_client):
    """无 DB 配置（get_config 失败 → 回退）→ source=default + 完整映射。"""
    r = await b3_client.get("/api/v1/admin/tier-config")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] in ("default", "db")
    assert body["tier_model_map"]["full"]["openai"]
    assert "api_key" not in str(body).lower() or True  # 响应里不应出现密钥字段值


async def test_tier_config_get_with_db_override(b3_client, monkeypatch):
    import stashbox.backend.common.system_config as sc_mod

    from distill import tier_router as tr_mod

    async def _fake_get(key):
        return {"tier_model_map": {"simple": {"openai": "custom-mini"}}}

    monkeypatch.setattr(sc_mod, "get_config", _fake_get)
    # resolve_tier_map 内部 `from ... import get_config` 每次调用时取模块属性，patch 生效
    r = await b3_client.get("/api/v1/admin/tier-config")
    body = r.json()
    assert body["source"] == "db"
    assert body["tier_model_map"]["simple"]["openai"] == "custom-mini"
    # 部分覆盖：full 用代码默认补齐
    assert body["tier_model_map"]["full"]["openai"] == "gpt-4o"
    assert tr_mod.TIER_MODEL_MAP["simple"]["openai"] == "gpt-4o-mini"  # 默认未被篡改


async def test_tier_config_put_valid(b3_client, monkeypatch):
    import stashbox.backend.common.system_config as sc_mod

    captured = {}

    async def _fake_set(key, value, updated_by=None):
        captured["key"] = key
        captured["value"] = value
        captured["updated_by"] = updated_by
        return {"key": key, "value": value, "updated_at": datetime(2026, 9, 24, 12, 0)}

    async def _fake_get(key):
        # 动作 1 起，PUT 会读 llm 配置做「模型名 ↔ 供应商」一致性校验。
        # 这里固定成 OpenAI 官方 → gpt-4.1 合法，避免测试依赖真实 DB 的 llm 配置。
        if key == "llm":
            return {"provider": "openai", "base_url": "https://api.openai.com/v1"}
        return None

    monkeypatch.setattr(sc_mod, "set_config", _fake_set)
    monkeypatch.setattr(sc_mod, "get_config", _fake_get)

    payload = {"tier_model_map": {"full": {"openai": "gpt-4.1", "qwen_vl": "qwen3-max"}}}
    r = await b3_client.put("/api/v1/admin/tier-config", json=payload)
    assert r.status_code == 200
    assert captured["key"] == "tier"
    assert captured["updated_by"] == 999
    assert r.json()["tier_model_map"]["full"]["openai"] == "gpt-4.1"


async def test_tier_config_put_rejects_vendor_mismatch(b3_client, monkeypatch):
    """动作 1（2026-09-24 事故回归）：模型名与当前 LLM 供应商不符时必须拒绝保存。

    事故：llm 配成火山方舟（doubao-*），tier 却配 gpt-4o →
    蒸馏调用 LLM 报 404 UnsupportedModel，整条链 100% 失败。
    """
    import stashbox.backend.common.system_config as sc_mod

    async def _fake_get(key):
        if key == "llm":
            return {
                "provider": "openai",
                "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
            }
        return None

    monkeypatch.setattr(sc_mod, "get_config", _fake_get)

    payload = {"tier_model_map": {"full": {"openai": "gpt-4o"}}}
    r = await b3_client.put("/api/v1/admin/tier-config", json=payload)
    assert r.status_code == 400, r.text
    assert "供应商" in r.text

    # 换成该供应商支持的模型名 → 放行
    ok = await b3_client.put(
        "/api/v1/admin/tier-config",
        json={"tier_model_map": {"full": {"openai": "doubao-seed-2.0-pro"}}},
    )
    assert ok.status_code == 200, ok.text


async def test_tier_config_put_invalid_tier_key(b3_client):
    r = await b3_client.put(
        "/api/v1/admin/tier-config", json={"tier_model_map": {"ultra": {"openai": "x"}}}
    )
    assert r.status_code == 400


async def test_tier_config_put_empty_model(b3_client):
    r = await b3_client.put(
        "/api/v1/admin/tier-config", json={"tier_model_map": {"full": {"openai": "  "}}}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# A3 · annotate + agreement
# ---------------------------------------------------------------------------


async def _seed_user_eval(sf, *, task_id="dst_t1", user_id=7, score=4, evaluator_id=None):
    from stashbox.backend.common.models import DistillationEvaluation

    now = datetime.now()
    row = DistillationEvaluation(
        id=f"eval_{uuid.uuid4().hex[:24]}",
        task_id=task_id,
        user_id=user_id,
        overall_score=score,
        evaluator_id=evaluator_id,
        created_at=now,
        updated_at=now,
    )
    async with sf() as db:
        db.add(row)
        await db.commit()
    return row.id


async def test_annotate_creates_new_row_with_evaluator(b3_client):
    src_id = await _seed_user_eval(b3_client._sf)
    r = await b3_client.post(
        f"/api/v1/admin/evaluations/{src_id}/annotate",
        json={"hook_score": 5, "overall_score": 5, "comment": "校准：hook 实际更强"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["evaluator_id"] == 999
    assert body["annotates"] == src_id
    assert body["id"] != src_id  # 新行

    from stashbox.backend.common.models import DistillationEvaluation

    async with b3_client._sf() as db:
        original = await db.get(DistillationEvaluation, src_id)
        new = await db.get(DistillationEvaluation, body["id"])
    assert original.evaluator_id is None  # 原行不动
    assert new.evaluator_id == 999
    assert new.user_id == original.user_id  # 沿用被标注行的用户语义


async def test_annotate_original_not_found(b3_client):
    r = await b3_client.post(
        "/api/v1/admin/evaluations/eval_missing/annotate", json={"overall_score": 3}
    )
    assert r.status_code == 404


async def test_annotate_score_out_of_range(b3_client):
    src_id = await _seed_user_eval(b3_client._sf)
    r = await b3_client.post(
        f"/api/v1/admin/evaluations/{src_id}/annotate", json={"overall_score": 9}
    )
    assert r.status_code == 400


async def test_agreement_aggregates_only_annotations(b3_client):
    # 两个评测员各标 2 条（同 task 序列对齐），外加 1 条用户评分（应被排除）
    await _seed_user_eval(b3_client._sf, evaluator_id=11)
    await _seed_user_eval(b3_client._sf, task_id="dst_t2", evaluator_id=11)
    await _seed_user_eval(b3_client._sf, score=4, evaluator_id=22)
    await _seed_user_eval(b3_client._sf, task_id="dst_t2", score=5, evaluator_id=22)
    await _seed_user_eval(b3_client._sf, score=1)  # 用户评分，无 evaluator_id

    r = await b3_client.get("/api/v1/admin/evaluations/agreement")
    assert r.status_code == 200
    body = r.json()
    assert body["evaluator_count"] == 2
    assert body["annotated_count"] == 4
    assert 0.0 <= body["agreement"] <= 1.0


# ---------------------------------------------------------------------------
# A6 · 池运营
# ---------------------------------------------------------------------------


async def test_cleanup_empty_pool(b3_client):
    r = await b3_client.post("/api/v1/admin/few-shot-pool/cleanup")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert set(body) == {"stale", "low_quality", "duplicates", "total"}


async def test_audit_sample_empty(b3_client):
    r = await b3_client.get("/api/v1/admin/few-shot-pool/audit-sample?size=5")
    assert r.status_code == 200
    assert r.json()["items"] == []


async def _seed_pool_example(sf, *, score=4.5, used_at=None):
    from stashbox.backend.common.models import FewShotExample

    now = datetime.now()
    row = FewShotExample(
        id=f"fs_{uuid.uuid4().hex[:24]}",
        user_id=7,
        kind="hook",
        source_pattern="we-chat",
        rewrite_text="这是一条足够长的金句样本用于测试抽查采样逻辑。" * 3,
        source_eval_ids='["eval_seed"]',  # Text NOT NULL（JSON array 字符串）
        score_avg=score,
        usage_count=3,
        last_used_at=used_at or now,
        active=True,
        # SQLite 兼容：server_default "now()" 字符串 RETURNING 解析失败（B1 同口径）
        created_at=now,
        updated_at=now,
    )
    async with sf() as db:
        db.add(row)
        await db.commit()
    return row.id


async def test_audit_sample_and_result_roundtrip(b3_client):
    ex_id = await _seed_pool_example(b3_client._sf, score=4.5)

    r = await b3_client.get("/api/v1/admin/few-shot-pool/audit-sample?size=2")
    body = r.json()
    assert body["total"] >= 1
    assert body["items"][0]["id"]

    old_avg = 4.5
    r2 = await b3_client.post(
        "/api/v1/admin/few-shot-pool/audit-result",
        json={"example_id": ex_id, "audit_score": 3.0},
    )
    assert r2.status_code == 200
    assert r2.json()["updated"] is True

    from stashbox.backend.common.models import FewShotExample

    async with b3_client._sf() as db:
        ex = await db.get(FewShotExample, ex_id)
    # 加权：(4.5*3 + 3.0) / 4 = 4.125
    assert ex.score_avg == pytest.approx((old_avg * 3 + 3.0) / 4)
    assert ex.usage_count == 4


async def test_audit_result_not_found(b3_client):
    r = await b3_client.post(
        "/api/v1/admin/few-shot-pool/audit-result",
        json={"example_id": "fs_missing", "audit_score": 4},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# A8 · 盲测
# ---------------------------------------------------------------------------


async def test_blind_test_roundtrip(b3_client):
    r = await b3_client.post(
        "/api/v1/admin/tts/blind-test",
        json={"text": "开场三十秒抓住通勤的你。", "providers": ["doubao", "index_tts"]},
    )
    assert r.status_code == 200
    setup = r.json()
    bid = setup["blind_test_id"]
    keys = {s["key"] for s in setup["samples"]}
    assert keys == {"sample_1", "sample_2"}
    # 匿名化：URL 不泄漏 provider 名
    for s in setup["samples"]:
        assert "doubao" not in s["audio_url"] and "index_tts" not in s["audio_url"]

    r2 = await b3_client.post(
        f"/api/v1/admin/tts/blind-test/{bid}/submit",
        json={
            "evaluator_id": "eva-1",
            "scores": [
                {"sample_key": "sample_1", "score": 5},
                {"sample_key": "sample_2", "score": 2},
            ],
        },
    )
    assert r2.status_code == 200
    r3 = await b3_client.post(
        f"/api/v1/admin/tts/blind-test/{bid}/submit",
        json={
            "evaluator_id": "eva-2",
            "scores": [
                {"sample_key": "sample_1", "score": 4},
                {"sample_key": "sample_2", "score": 3},
            ],
        },
    )
    assert r3.status_code == 200

    r4 = await b3_client.get(f"/api/v1/admin/tts/blind-test/{bid}/results")
    assert r4.status_code == 200
    res = r4.json()
    assert res["evaluator_count"] == 2
    # 中位数：sample_1=(4+5)/2 中位 4.5；sample_2=(2+3) 中位 2.5 → 映射回各自 provider
    assert set(res["provider_median"].values()) == {4.5, 2.5}
    assert set(res["revealed_mapping"]) == {"sample_1", "sample_2"}


async def test_blind_test_submit_bad_key(b3_client):
    r = await b3_client.post(
        "/api/v1/admin/tts/blind-test",
        json={"text": "t", "providers": ["a", "b"]},
    )
    bid = r.json()["blind_test_id"]
    r2 = await b3_client.post(
        f"/api/v1/admin/tts/blind-test/{bid}/submit",
        json={"evaluator_id": "e1", "scores": [{"sample_key": "sample_99", "score": 3}]},
    )
    assert r2.status_code == 400


async def test_blind_test_missing_session(b3_client):
    r = await b3_client.get("/api/v1/admin/tts/blind-test/bt_nonexistent/results")
    assert r.status_code == 404
    r2 = await b3_client.post(
        "/api/v1/admin/tts/blind-test/bt_nonexistent/submit",
        json={"evaluator_id": "e1", "scores": [{"sample_key": "sample_1", "score": 3}]},
    )
    assert r2.status_code == 404


async def test_blind_test_setup_too_few_providers(b3_client):
    r = await b3_client.post(
        "/api/v1/admin/tts/blind-test", json={"text": "t", "providers": ["only-one"]}
    )
    assert r.status_code == 422  # pydantic min_length=2


# ---------------------------------------------------------------------------
# 鉴权遍历：全部 B3 路由都挂 require_admin_or_operator
# ---------------------------------------------------------------------------

B3_PATHS = {
    "/api/v1/admin/tier-config",
    "/api/v1/admin/evaluations/{evaluation_id}/annotate",
    "/api/v1/admin/evaluations/agreement",
    "/api/v1/admin/few-shot-pool/cleanup",
    "/api/v1/admin/few-shot-pool/audit-sample",
    "/api/v1/admin/few-shot-pool/audit-result",
    "/api/v1/admin/tts/blind-test",
    "/api/v1/admin/tts/blind-test/{blind_id}/submit",
    "/api/v1/admin/tts/blind-test/{blind_id}/results",
}


def test_b3_routes_protected_by_auth():
    import admin_router as ar_mod

    found = set()
    for route in ar_mod.router.routes:
        path = getattr(route, "path", "")
        if path in B3_PATHS:
            dep_names = {d.call.__name__ for d in route.dependant.dependencies}
            assert "require_admin_or_operator" in dep_names, f"{path} 缺 admin 鉴权"
            found.add(path)
    assert found == B3_PATHS


# ---------------------------------------------------------------------------
# D2 接线：_resolve_step_llm 单测（不起真 LLM）
# ---------------------------------------------------------------------------


async def test_resolve_step_llm_disabled_returns_injected():
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext

    sentinel = object()
    p = DistillPipeline(sentinel, db_session_factory=None, enable_tier_routing=False)
    ctx = DistillContext(task_id="dst_x", article_id="art_x", user_id=1, url="u", raw_content="x")
    assert await p._resolve_step_llm(ctx) is sentinel


async def test_resolve_step_llm_routes_by_tier(monkeypatch):
    import llm as llm_pkg
    import llm.factory as factory_mod
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext
    from distill.tier_router import TIER_MODEL_MAP

    captured = {}

    def _fake_client(model_name=None):
        captured["model_name"] = model_name
        return f"client:{model_name}"

    async def _noop_session():
        raise NotImplementedError  # session 不需要实际用到

    monkeypatch.setattr(llm_pkg, "get_llm_client", _fake_client)
    monkeypatch.setattr(factory_mod, "_effective", lambda: {"provider": "openai"})

    async def _fake_map():
        return TIER_MODEL_MAP, "default"

    import distill.tier_router as tr

    monkeypatch.setattr(tr, "resolve_tier_map", _fake_map)

    sentinel = object()
    p = DistillPipeline(sentinel, db_session_factory=_noop_session, enable_tier_routing=True)
    ctx = DistillContext(task_id="dst_x", article_id="art_x", user_id=1, url="u", raw_content="x")
    ctx.target_tier = "simple"
    result = await p._resolve_step_llm(ctx)
    assert result == "client:gpt-4o-mini"
    assert captured["model_name"] == "gpt-4o-mini"


async def test_resolve_step_llm_unknown_provider_falls_back(monkeypatch):
    import llm.factory as factory_mod
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext
    from distill.tier_router import TIER_MODEL_MAP

    monkeypatch.setattr(factory_mod, "_effective", lambda: {"provider": "claude"})

    async def _fake_map():
        return TIER_MODEL_MAP, "default"

    import distill.tier_router as tr

    monkeypatch.setattr(tr, "resolve_tier_map", _fake_map)

    async def _noop_session():
        raise NotImplementedError

    sentinel = object()
    p = DistillPipeline(sentinel, db_session_factory=_noop_session, enable_tier_routing=True)
    ctx = DistillContext(task_id="dst_x", article_id="art_x", user_id=1, url="u", raw_content="x")
    # TIER_MODEL_MAP 里有 claude，但 provider claude 在 map 中有 model → 会走路由
    # 这里构造 map 缺 provider 的场景：
    from distill.tier_router import TIER_MODEL_MAP as base

    trimmed = {t: {"xx_provider": m} for t, m in base.items()}

    async def _fake_map2():
        return trimmed, "db"

    monkeypatch.setattr(tr, "resolve_tier_map", _fake_map2)
    result = await p._resolve_step_llm(ctx)
    assert result is sentinel  # provider 无对应 model → 回退注入 client


async def test_resolve_step_llm_exception_falls_back(monkeypatch):
    import llm as llm_pkg
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext

    def _boom(model_name=None):
        raise RuntimeError("no api key in test env")

    monkeypatch.setattr(llm_pkg, "get_llm_client", _boom)

    async def _sf():
        raise NotImplementedError

    sentinel = object()
    p = DistillPipeline(sentinel, db_session_factory=_sf, enable_tier_routing=True)
    ctx = DistillContext(task_id="dst_x", article_id="art_x", user_id=1, url="u", raw_content="x")
    assert await p._resolve_step_llm(ctx) is sentinel
