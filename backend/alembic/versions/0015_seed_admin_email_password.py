"""CP-ADMIN-FIX seed admin email + password（CP3.6.2-XIN 后端补齐）

admin-web 用 email + password 登录（POST /api/v1/admin/auth/login），
但 0006_seed_admin_user 只填了 open_id='admin_seed' + tier='admin'，
email + password_hash 是 NULL（0006 时还没加这 2 个字段——0010 才加）。

修法：给 admin_seed 加 email + bcrypt password_hash。

密码来源（按优先级）：
  1. 环境变量 ADMIN_BOOTSTRAP_PASSWORD     ← 生产/部署必须用这个
  2. 默认值 'admin@stashbox123'           ← 仅供本地 dev；启动会打 WARNING 提示

正式上线必须用 ADMIN_BOOTSTRAP_PASSWORD 注入（见 restart-*.sh / deploy 文档）。
"""

import os

import bcrypt
from alembic import op

revision = "0015"
down_revision = "0014"  # 上一段是 CP5.5-A3 feedback_v2
branch_labels = None
depends_on = None


_DEFAULT_DEV_PASSWORD = "admin@stashbox123"


def upgrade() -> None:
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "").strip() or _DEFAULT_DEV_PASSWORD
    if password == _DEFAULT_DEV_PASSWORD:
        # 不阻塞 seed（本地 dev 必须能跑），但必须让运维警觉。
        import warnings

        warnings.warn(
            "[0015] 使用默认 dev 密码。生产部署请通过 ADMIN_BOOTSTRAP_PASSWORD "
            "环境变量注入强密码，否则任何人可登录 admin-web。",
            stacklevel=2,
        )
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
