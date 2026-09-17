"""CP5.6 admin CSV 数据导出 5 端点单测（v1 §11.5）。

覆盖：
  GET /api/v1/admin/export/users.csv        ：200 + CSV 头 + 行数 + BOM + audit log
  GET /api/v1/admin/export/articles.csv     ：200 + CSV 头 + 标签列（distilled LEFT JOIN）
  GET /api/v1/admin/export/feedback.csv     ：200 + CSV 头 + seed 行
  GET /api/v1/admin/export/audit-log.csv    ：200 + CSV 头 + 含本次导出日志
  GET /api/v1/admin/export/subscriptions.csv：200 + CSV 头（表未实现 -> header-only 降级）
  鉴权：非 admin 403 / 未登录 401

依赖真实 PG。表由 fixture 幂等建（绕过 broken 0008 迁移链，仅建本测试相关表）。
"""
import csv
import importlib.util
import io
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
    Feedback,
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


content_module, content_app = _load_app("_cp56_csv_export_main", "content-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_tables():
    """幂等建本测试依赖的 5 张表（users / articles / distilled_articles / feedback / logs）。"""
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Article.__table__,
                DistilledArticle.__table__,
                Feedback.__table__,
                AdminOperationLog.__table__,
            ],
        )
    yield


async def _make_user(tier: str = "admin", nickname: str | None = None) -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp56_" + uuid.uuid4().hex[:24],
            nickname=nickname or ("u_" + uuid.uuid4().hex[:6]),
            email=f"{uuid.uuid4().hex[:8]}@cp56.test",
            tier=tier,
            monthly_quota=42,
            quota_used=7,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


async def _make_article(user_id: int, tags: list | None = None) -> str:
    """建 article + distilled_articles（tags / quality_score 用于 articles.csv 列断言）。"""
    async with AsyncSessionLocal() as s:
        a = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url="https://example.com/cp56",
            title="CP5.6 导出用例",
            source="d9",
            status="ready",
            favorite=False,
            skip=False,
        )
        s.add(a)
        await s.flush()
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=a.id,
                status="done",
                tags=tags if tags is not None else ["科技", "商业"],
                quality_score=8.5,
            )
        )
        await s.commit()
        return a.id


async def _make_feedback(user_id: int, article_id: str) -> int:
    async with AsyncSessionLocal() as s:
        f = Feedback(
            user_id=user_id,
            article_id=article_id,
            type="rating",
            rating=5,
            reason="good",
            metadata_={"src": "cp56"},
        )
        s.add(f)
        await s.commit()
        await s.refresh(f)
        return int(f.id)


def _token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


def _parse_csv(resp) -> list[list[str]]:
    """解码 CSV（剥 BOM）为二维列表。"""
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))


async def _get(path: str, headers: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


# 1. users.csv：200 + CSV Content-Type + header + seed 行 + BOM + audit log
@pytest.mark.asyncio
async def test_export_users_csv():
    admin = await _make_user(tier="admin", nickname="cp56_admin_users")
    resp = await _get("/api/v1/admin/export/users.csv", {"Authorization": f"Bearer {_token(admin)}"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert ".csv" in resp.headers["content-disposition"]

    # UTF-8 BOM（Excel 打开中文不乱码）
    assert resp.content.startswith(b"\xef\xbb\xbf")

    rows = _parse_csv(resp)
    assert rows[0] == [
        "id", "email", "display_name", "role", "tier", "status",
        "monthly_quota", "used_quota", "last_active_at", "created_at",
    ]
    assert len(rows) >= 2  # header + 至少 seed 的 admin

    mine = [r for r in rows[1:] if r[0] == str(admin)]
    assert len(mine) == 1
    row = mine[0]
    assert row[2] == "cp56_admin_users"  # display_name <- nickname
    assert row[3] == row[4] == "admin"  # role / tier
    assert row[5] == "active"
    assert row[6] == "42" and row[7] == "7"  # monthly_quota / used_quota

    # 每次导出写一条 admin_operation_logs（target_id 形如 users-YYYY-MM-DD.csv）
    async with AsyncSessionLocal() as s:
        log = (
            await s.execute(
                text(
                    "SELECT target_id, request_body FROM admin_operation_logs "
                    "WHERE admin_id = :aid AND action = 'ADMIN_EXPORT' "
                    "AND target_type = 'export' AND target_id LIKE 'users-%.csv'"
                ),
                {"aid": admin},
            )
        ).all()
    assert len(log) == 1
    assert log[0][1]["filename"] == log[0][0]
    assert log[0][1]["row_count"] == len(rows) - 1  # 不含 header


# 2. articles.csv：200 + header + distilled 标签/质量分列
@pytest.mark.asyncio
async def test_export_articles_csv():
    admin = await _make_user()
    uid = await _make_user(tier="free")
    art_id = await _make_article(uid, tags=["科技", "商业"])
    resp = await _get(
        "/api/v1/admin/export/articles.csv", {"Authorization": f"Bearer {_token(admin)}"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _parse_csv(resp)
    assert rows[0] == [
        "id", "user_id", "title", "source", "url", "status",
        "tags", "quality_score", "listened_at", "created_at",
    ]
    assert len(rows) >= 2

    mine = [r for r in rows[1:] if r[0] == art_id]
    assert len(mine) == 1
    assert mine[0][1] == str(uid)
    assert mine[0][5] == "ready"
    assert mine[0][6] == "科技|商业"  # distilled_articles.tags
    assert mine[0][7] == "8.5"  # distilled_articles.quality_score


# 3. feedback.csv：200 + header + seed 行
@pytest.mark.asyncio
async def test_export_feedback_csv():
    admin = await _make_user()
    uid = await _make_user(tier="free")
    art_id = await _make_article(uid)
    fb_id = await _make_feedback(uid, art_id)
    resp = await _get(
        "/api/v1/admin/export/feedback.csv", {"Authorization": f"Bearer {_token(admin)}"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _parse_csv(resp)
    assert rows[0] == [
        "id", "user_id", "article_id", "type", "rating", "reason", "metadata", "created_at",
    ]
    mine = [r for r in rows[1:] if r[0] == str(fb_id)]
    assert len(mine) == 1
    assert mine[0][1] == str(uid)
    assert mine[0][2] == art_id
    assert mine[0][3] == "rating"
    assert mine[0][4] == "5"


# 4. audit-log.csv：200 + header + 含本次导出写的那条日志
@pytest.mark.asyncio
async def test_export_audit_log_csv():
    admin = await _make_user()
    resp = await _get(
        "/api/v1/admin/export/audit-log.csv", {"Authorization": f"Bearer {_token(admin)}"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _parse_csv(resp)
    assert rows[0] == [
        "id", "actor_id", "action_type", "target_type", "target_id", "payload", "created_at",
    ]
    assert len(rows) >= 2  # header + 本次导出日志
    assert any(
        r[2] == "ADMIN_EXPORT" and r[3] == "export" and r[4].startswith("audit-log-")
        for r in rows[1:]
    )


# 5. subscriptions.csv：200 + header（表未实现 -> header-only 降级，非 500）
@pytest.mark.asyncio
async def test_export_subscriptions_csv():
    admin = await _make_user()
    resp = await _get(
        "/api/v1/admin/export/subscriptions.csv", {"Authorization": f"Bearer {_token(admin)}"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _parse_csv(resp)
    assert rows[0] == [
        "id", "user_id", "tier", "started_at", "expires_at", "status", "auto_renew",
    ]
    assert len(rows) >= 1  # 至少 header；表存在时还有数据行


# 6. 非 admin / operator -> 403
@pytest.mark.asyncio
async def test_export_forbidden_for_non_admin():
    free = await _make_user(tier="free")
    resp = await _get(
        "/api/v1/admin/export/users.csv", {"Authorization": f"Bearer {_token(free, tier='free')}"}
    )
    assert resp.status_code == 403


# 7. 未登录 -> 401
@pytest.mark.asyncio
async def test_export_requires_auth():
    resp = await _get("/api/v1/admin/export/users.csv")
    assert resp.status_code == 401
