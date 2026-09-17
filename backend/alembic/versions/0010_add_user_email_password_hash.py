"""add users.email + users.password_hash（CP3.6.2-XIN admin login）。

- email: VARCHAR(255) NULL（匿名用户无 email；建索引加速按 email 查 admin）
- password_hash: VARCHAR(255) NULL（匿名用户无密码；bcrypt hash）

admin login 端点按 email 查用户 + bcrypt 校验 password_hash，再签发 1h JWT。
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(255), nullable=True))
    op.create_index("idx_users_email", "users", ["email"])
    op.add_column(
        "users", sa.Column("password_hash", sa.String(255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "password_hash")
    op.drop_index("idx_users_email", table_name="users")
    op.drop_column("users", "email")
