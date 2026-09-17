"""tags 表 + GET /api/v1/tags DB 化（CP5.3a）。v1 §11.5 CP5.3 后端基础。

前置条件：alembic upgrade 0007 已跑（dev DB 升级由 Hornet 手动执行）。
"""
import pytest
from sqlalchemy import text
from stashbox.backend.common.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_tags_table_exists():
    """alembic 0007 后 tags 表存在 + 4 个 seed（tech/finance/life/news）"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT slug, name, category FROM tags ORDER BY slug"))
        rows = r.fetchall()
        assert len(rows) >= 4, f"tags 表只有 {len(rows)} 行（期望 ≥ 4 seed）"


@pytest.mark.asyncio
async def test_tag_subscriptions_table_exists():
    """tag_subscriptions 表存在"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT 1 FROM tag_subscriptions LIMIT 1"))
        # 不管返不返 rows，只确认表存在
        # 如果表不存在 SQLAlchemy 抛 ProgrammingError
