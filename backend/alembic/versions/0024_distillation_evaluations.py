"""CP3.7.1 §2.1.A：distillation_evaluations 表（4 维听感评分）

听感产品化数据底座第 1 张表。CP3.7.3 PostDistillHook 用：
- overall_score <= 2 触发自动重蒸（auto_flag=True）
- overall_score >= 4 入选 few-shot 池
- 4 维评分用于监听 prompt / 模型降级

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "distillation_evaluations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("hook_score", sa.SmallInteger(), nullable=True),
        sa.Column("section_score", sa.SmallInteger(), nullable=True),
        sa.Column("outro_score", sa.SmallInteger(), nullable=True),
        sa.Column("rhythm_score", sa.SmallInteger(), nullable=True),
        sa.Column("overall_score", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("auto_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("retried_task_id", sa.String(length=32), nullable=True),
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
        sa.ForeignKeyConstraint(["task_id"], ["distilled_articles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("overall_score BETWEEN 1 AND 5", name="ck_eval_overall_score_range"),
        sa.CheckConstraint(
            "(hook_score IS NULL OR hook_score BETWEEN 1 AND 5)", name="ck_eval_hook_score_range"
        ),
        sa.CheckConstraint(
            "(section_score IS NULL OR section_score BETWEEN 1 AND 5)",
            name="ck_eval_section_score_range",
        ),
        sa.CheckConstraint(
            "(outro_score IS NULL OR outro_score BETWEEN 1 AND 5)",
            name="ck_eval_outro_score_range",
        ),
        sa.CheckConstraint(
            "(rhythm_score IS NULL OR rhythm_score BETWEEN 1 AND 5)",
            name="ck_eval_rhythm_score_range",
        ),
    )
    op.create_index("idx_eval_task", "distillation_evaluations", ["task_id"])
    op.create_index("idx_eval_user_score", "distillation_evaluations", ["user_id", "overall_score"])


def downgrade() -> None:
    op.drop_index("idx_eval_user_score", table_name="distillation_evaluations")
    op.drop_index("idx_eval_task", table_name="distillation_evaluations")
    op.drop_table("distillation_evaluations")
