"""admin seed migration 验证（CP1.8.1）。"""
import pytest
from sqlalchemy import text
from stashbox.backend.common.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_admin_seed_migration_creates_admin_user():
    """alembic 0006 upgrade 后：至少有 1 个 tier=admin 用户"""
    # 验证表里存在 open_id='admin_seed' + tier='admin' 的用户
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            text("SELECT tier FROM users WHERE open_id = 'admin_seed'")
        )
        row = r.first()
        assert row is not None, "admin_seed 用户不存在"
        assert row[0] == "admin", f"admin_seed tier 错: {row[0]}"
