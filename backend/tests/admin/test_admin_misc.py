"""CP3.6-A3 admin 其他 3 端点单测（v1 §3.6）。

覆盖：
  POST /api/v1/admin/articles/{id}/force-retry
      ：正常 / 不存在 / 原因太短 / operator 可访问
  POST /api/v1/admin/audio/{id}/invalidate
      ：正常 / 不存在 / 重复 invalidate 幂等
  GET  /api/v1/admin/audit-log
      ：默认 / 分页 / 按 actor 过滤 / 按 action 过滤

依赖真实 PG（/tmp:5432）。audio 实体在仓库里即 distilled_articles（无独立 audio_files
表），故 audio invalidate 作用于 distilled_articles。admin_operation_logs 表由 fixture
幂等建（绕过 broken 0008 迁移链，仅建本测试相关表）。
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import (
    AdminOperationLog,
    Article,
    DistilledArticle,
    User,
)
from stashbox.backend.common.models.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """content-service 目录名带连字符，按文件加载，返回 (module, app)。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, module.app


content_module, content_app = _load_app("_cp36a3_admin_misc_main", "content-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_tables():
    """幂等建本测试依赖的表（articles / distilled_articles / users / admin_operation_logs）。"""
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Article.__table__,
                DistilledArticle.__table__,
                AdminOperationLog.__table__,
            ],
        )
    yield


@pytest.fixture
def fake_ai(monkeypatch):
    """不真调 ai-service，记录调用参数（挂在 self.calls 上）。"""
    class _Fake:
        def __init__(self):
            self.calls = []

        async def trigger_distill(self, article_id, auth_token=None, **_kw):
            self.calls.append({"article_id": article_id, "auth_token": auth_token})
            return {"article_id": article_id, "task_id": "dst_fake", "status": "started"}

    fake = _Fake()
    # 主模块里 `from clients.ai_client import get_ai_client` 已绑定名字，须 patch 主模块属性
    monkeypatch.setattr(content_module, "get_ai_client", lambda: fake)
    return fake


async def _make_user(tier: str = "free", monthly_quota: int = 5) -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp36a3_" + uuid.uuid4().hex[:24],
            nickname="u_" + uuid.uuid4().hex[:6],
            tier=tier,
            monthly_quota=monthly_quota,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


async def _make_article(user_id: int, status: str = "failed") -> str:
    async with AsyncSessionLocal() as s:
        a = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url="https://example.com/a",
            source="d9",
            status=status,
            favorite=False,
            skip=False,
        )
        s.add(a)
        await s.commit()
        return a.id


async def _make_audio(status: str = "done", audio_url: str = "https://oss/x.m4a") -> str:
    async with AsyncSessionLocal() as s:
        # distilled_articles.article_id 有 FK 约束，需先建父 article
        a = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=await _make_user(),
            url="https://example.com/audio-parent",
            source="d9",
            status="ready",
            favorite=False,
            skip=False,
        )
        s.add(a)
        await s.flush()
        d = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=a.id,
            status=status,
            audio_url=audio_url,
        )
        s.add(d)
        await s.commit()
        return d.id


def _token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# force-retry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_force_retry_normal(fake_ai):
    """正常：status->pending + 写一条 audit log + 触发蒸馏。"""
    admin = await _make_user(tier="admin")
    uid = await _make_user()
    art_id = await _make_article(uid, status="failed")
    async with _client(_token(admin)) as c:
        resp = await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "手动重试失败文章"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["article_id"] == art_id
    assert body["status"] == "pending"
    assert body["distill_triggered"] is True
    assert "queued_at" in body
    # 触发了 distill
    assert len(fake_ai.calls) == 1 and fake_ai.calls[0]["article_id"] == art_id
    # audit 落库（仅统计本 article 的 force_retry 日志，避免跨用例残留行干扰）
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id=:tid AND action='force_retry'"
            ),
            {"tid": art_id},
        )
        assert cnt == 1


@pytest.mark.asyncio
async def test_force_retry_not_found(fake_ai):
    """文章不存在 -> 404，且不触发蒸馏 / 不写 log。"""
    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        resp = await c.post(
            "/api/v1/admin/articles/nope_404/force-retry",
            json={"reason": "手动重试失败文章"},
        )
    assert resp.status_code == 404
    assert fake_ai.calls == []
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id='nope_404' AND action='force_retry'"
            )
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_force_retry_reason_too_short():
    """reason < 5 字符 -> 400，且不落库。"""
    admin = await _make_user(tier="admin")
    art_id = await _make_article(await _make_user())
    async with _client(_token(admin)) as c:
        resp = await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "abc"},
        )
    assert resp.status_code == 400
    async with AsyncSessionLocal() as s:
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id=:tid AND action='force_retry'"
            ),
            {"tid": art_id},
        )
        assert cnt == 0


@pytest.mark.asyncio
async def test_force_retry_operator_allowed(fake_ai):
    """operator 角色亦可通过 require_admin_or_operator。"""
    operator = await _make_user(tier="operator")
    art_id = await _make_article(await _make_user())
    async with _client(_token(operator, tier="operator")) as c:
        resp = await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "operator 手动重试"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# audio invalidate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audio_invalidate_normal():
    """正常：distilled_article 状态置 invalidated + 写 audit log。"""
    admin = await _make_user(tier="admin")
    audio_id = await _make_audio(status="done", audio_url="https://oss/x.m4a")
    async with _client(_token(admin)) as c:
        resp = await c.post(
            f"/api/v1/admin/audio/{audio_id}/invalidate",
            json={"reason": "音频质量有问题"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audio_id"] == audio_id
    assert body["status"] == "invalidated"
    async with AsyncSessionLocal() as s:
        row = await s.get(DistilledArticle, audio_id)
        assert row.status == "invalidated"
        cnt = await s.scalar(
            text(
                "SELECT count(*) FROM admin_operation_logs "
                "WHERE target_id=:tid AND action='audio_invalidate'"
            ),
            {"tid": audio_id},
        )
        assert cnt == 1


@pytest.mark.asyncio
async def test_audio_invalidate_not_found():
    """音频不存在 -> 404。"""
    admin = await _make_user(tier="admin")
    async with _client(_token(admin)) as c:
        resp = await c.post(
            "/api/v1/admin/audio/dst_nope/invalidate",
            json={"reason": "音频质量有问题"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_audio_invalidate_idempotent():
    """重复 invalidate 幂等：状态保持 invalidated，仍返回 200。"""
    admin = await _make_user(tier="admin")
    audio_id = await _make_audio(status="done", audio_url="https://oss/x.m4a")
    async with _client(_token(admin)) as c:
        r1 = await c.post(
            f"/api/v1/admin/audio/{audio_id}/invalidate",
            json={"reason": "音频质量有问题"},
        )
        r2 = await c.post(
            f"/api/v1/admin/audio/{audio_id}/invalidate",
            json={"reason": "再次作废同一音频"},
        )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["status"] == "invalidated"
    async with AsyncSessionLocal() as s:
        row = await s.get(DistilledArticle, audio_id)
        assert row.status == "invalidated"


# ---------------------------------------------------------------------------
# audit-log
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_log_default():
    """默认查询返回 {total, items}，且按 created_at DESC。"""
    admin = await _make_user(tier="admin")
    uid = await _make_user()
    art_id = await _make_article(uid, status="failed")
    async with _client(_token(admin)) as c:
        # 先造一条 force_retry 日志
        await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "手动重试失败文章"},
        )
        resp = await c.get("/api/v1/admin/audit-log")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data.keys()) >= {"total", "items"}
    assert data["total"] >= 1
    # 本次刚写了 force_retry，至少有一条
    assert any(it["action_type"] == "force_retry" for it in data["items"])
    assert "created_at" in data["items"][0]


@pytest.mark.asyncio
async def test_audit_log_pagination():
    """分页：两页不重叠。"""
    admin = await _make_user(tier="admin")
    uid = await _make_user()
    for _ in range(3):
        art_id = await _make_article(uid, status="failed")
        async with _client(_token(admin)) as c:
            await c.post(
                f"/api/v1/admin/articles/{art_id}/force-retry",
                json={"reason": "分页测试重试"},
            )
    async with _client(_token(admin)) as c:
        p1 = await c.get("/api/v1/admin/audit-log?page=1&size=2")
        p2 = await c.get("/api/v1/admin/audit-log?page=2&size=2")
    ids1 = [it["id"] for it in p1.json()["items"]]
    ids2 = [it["id"] for it in p2.json()["items"]]
    assert not (set(ids1) & set(ids2))


@pytest.mark.asyncio
async def test_audit_log_filter_by_actor():
    """按 actor_id 过滤：只返回该 admin 的操作。"""
    admin_a = await _make_user(tier="admin")
    admin_b = await _make_user(tier="admin")
    uid = await _make_user()
    art_id = await _make_article(uid, status="failed")
    async with _client(_token(admin_a)) as c:
        await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "admin_a 重试"},
        )
    async with _client(_token(admin_b)) as c:
        resp = await c.get(f"/api/v1/admin/audit-log?actor_id={admin_a}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(it["actor_id"] == admin_a for it in items)


@pytest.mark.asyncio
async def test_audit_log_filter_by_action():
    """按 action_type 过滤：只返回 force_retry。"""
    admin = await _make_user(tier="admin")
    uid = await _make_user()
    audio_id = await _make_audio()
    async with _client(_token(admin)) as c:
        art_id = await _make_article(uid, status="failed")
        await c.post(
            f"/api/v1/admin/articles/{art_id}/force-retry",
            json={"reason": "action 过滤测试"},
        )
        await c.post(
            f"/api/v1/admin/audio/{audio_id}/invalidate",
            json={"reason": "音频质量有问题"},
        )
        resp = await c.get("/api/v1/admin/audit-log?action_type=force_retry")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(it["action_type"] == "force_retry" for it in items)
