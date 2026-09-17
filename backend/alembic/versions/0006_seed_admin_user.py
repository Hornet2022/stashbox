"""seed 1 admin 用户（CP1.8.1）。v1 §3.6 admin 角色 seed。

Hornet 一期 owner 兼任 admin。open_id = 'admin_seed'（不与微信 openid 冲突）。
INSERT 而非 UPDATE：不破坏现有 users 行。
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # INSERT admin 用户（如果已存在则跳过——幂等）
    op.execute(
        """
        INSERT INTO users (
            open_id, tier, nickname, avatar_url, monthly_quota, quota_used, quota_version, created_at, updated_at
        )
        SELECT 'admin_seed', 'admin', 'Hornet (Admin)', '', -1, 0, 1, NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE open_id = 'admin_seed')
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM users WHERE open_id = 'admin_seed'")
