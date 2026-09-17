"""add quota fields: users.monthly_quota / quota_used / quota_version / quota_reset_at

CP1.6（v1 §4.2.1 + §4.10）：配额扣减事务（乐观锁）需要这 4 个字段。
与 0001 一致，本迁移手写（本机无 Docker，autogenerate 需连 PG）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "monthly_quota",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.add_column(
        "users",
        sa.Column("quota_used", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("quota_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("users", sa.Column("quota_reset_at", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "quota_reset_at")
    op.drop_column("users", "quota_version")
    op.drop_column("users", "quota_used")
    op.drop_column("users", "monthly_quota")
