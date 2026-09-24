"""CP5.6.0 §3.1：user_consents 表（用户同意记录）

个性化 + 跨用户金句 + 同意版本号（隐私政策 v2）。
GDPR 合规必备：用户拒绝时不再个性化 / 跨用户金句复用。

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_consents",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "personalization_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "cross_user_share_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("consent_at", sa.String(length=32), nullable=True),
        sa.Column(
            "consent_version",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'v2'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_consents")
