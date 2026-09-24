"""B4（修 D1 + 决策 §5.2 方案 B）：A/B 归因数据底座

1. distilled_articles 加 ab_group VARCHAR(16) NULL + is_personalized BOOLEAN NULL
   - 方案 §2.7-D：user_id % 100 < 30 → personalized 组，其余 general 组
   - NULL = 0029 上线前的历史数据（未实验期），ab-report 按 NULL 单独一组
2. distillation_evaluations 加 evaluator_id BIGINT NULL（FK users.id）
   - A3 评测员标注端点（B3）用它记录评测员归属，user_id 保持原语义不强塞 admin_id

三列全部 nullable，不动旧数据。

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "distilled_articles",
        sa.Column("ab_group", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "distilled_articles",
        sa.Column("is_personalized", sa.Boolean(), nullable=True),
    )
    op.create_index("idx_distilled_ab_group", "distilled_articles", ["ab_group"])
    op.add_column(
        "distillation_evaluations",
        sa.Column("evaluator_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_eval_evaluator",
        "distillation_evaluations",
        "users",
        ["evaluator_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_eval_evaluator", "distillation_evaluations", type_="foreignkey")
    op.drop_column("distillation_evaluations", "evaluator_id")
    op.drop_index("idx_distilled_ab_group", table_name="distilled_articles")
    op.drop_column("distilled_articles", "is_personalized")
    op.drop_column("distilled_articles", "ab_group")
