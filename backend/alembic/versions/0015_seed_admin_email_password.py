"""CP-ADMIN-FIX seed admin email + password（CP3.6.2-XIN 后端补齐）

admin-web 用 email + password 登录（POST /api/v1/admin/auth/login），
但 0006_seed_admin_user 只填了 open_id='admin_seed' + tier='admin'，
email + password_hash 是 NULL（0006 时还没加这 2 个字段——0010 才加）。

修法：给 admin_seed 加 email + bcrypt password_hash。

默认密码：admin@stashbox123（dev 用，正式上线前必须改）。
"""
import bcrypt
from alembic import op

revision = "0015"
down_revision = "0014"  # 上一段是 CP5.5-A3 feedback_v2
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 默认 dev 密码：admin@stashbox123
    password = "admin@stashbox123"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    op.execute(
        f"""
        UPDATE users
        SET email = 'admin@stashbox.local',
            password_hash = '{password_hash}',
            updated_at = NOW()
        WHERE open_id = 'admin_seed'
          AND (email IS NULL OR password_hash IS NULL)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE users
        SET email = NULL,
            password_hash = NULL
        WHERE open_id = 'admin_seed'
        """
    )
