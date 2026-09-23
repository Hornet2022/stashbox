"""CP3.7.1 §2.1.B：user_listening_patterns 表（用户听感画像）

个性化改写（CP5.6）的输入。
CP3.7.3 PostDistillHook 增量更新（30 篇滑动窗口 + 加权平均）。
feedback_count < 5 时所有画像字段保持 NULL（冷启动保护）。

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_listening_patterns",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("feedback_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("avg_session_sec", sa.Integer(), nullable=True),
        sa.Column("skip_rate", sa.REAL(), nullable=True),
        sa.Column("completion_rate", sa.REAL(), nullable=True),
        sa.Column("preferred_rhythm", sa.String(length=16), nullable=True),
        sa.Column("preferred_hook_type", sa.String(length=16), nullable=True),
        sa.Column("avg_overall_score", sa.REAL(), nullable=True),
        sa.Column("last_distill_at", sa.String(length=32), nullable=True),
        sa.Column(
            "last_updated",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
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
    op.create_index("idx_ulp_updated", "user_listening_patterns", ["last_updated"])


def downgrade() -> None:
    op.drop_index("idx_ulp_updated", table_name="user_listening_patterns")
    op.drop_table("user_listening_patterns")
