"""CP3.7.1 §2.1.D：few_shot_examples 表（高分改写片段库）

CP3.7.3 PostDistillHook 自动入选（overall_score >= 4 时调用 add_high_score_to_pool）。
Step 2 prompt 注入前 select_few_shot 取全局池 + 个人池 top-5。
1000 条 LRU 上限（CP3.7.3 落地）。

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "few_shot_examples",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),  # NULL = 全局池
        sa.Column("source_pattern", sa.String(length=64), nullable=False),
        sa.Column("rewrite_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("score_avg", sa.Float(), nullable=False),
        sa.Column("source_eval_ids", sa.Text(), nullable=False),  # JSON array 字符串
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('hook', 'section', 'outro')", name="ck_fs_kind_enum"),
        sa.CheckConstraint("score_avg BETWEEN 1 AND 5", name="ck_fs_score_range"),
    )
    op.create_index("idx_fs_active_score", "few_shot_examples", ["active", "score_avg"])
    op.create_index("idx_fs_user_kind", "few_shot_examples", ["user_id", "kind", "active"])
    op.create_index("idx_fs_pattern", "few_shot_examples", ["source_pattern", "active"])


def downgrade() -> None:
    op.drop_index("idx_fs_pattern", table_name="few_shot_examples")
    op.drop_index("idx_fs_user_kind", table_name="few_shot_examples")
    op.drop_index("idx_fs_active_score", table_name="few_shot_examples")
    op.drop_table("few_shot_examples")
