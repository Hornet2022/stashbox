"""create feedback table for analytics events (CP6.2.1, v1 §4.3.4)

CP6.2.1: 数据埋点 SDK 后端基础表，type 字段枚举核心事件名。
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("article_id", sa.String(32), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),  # CP6.2.1 改成 32（兼容未来事件名）
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),  # 兼容更长 reason
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_feedback_user_type",
        "feedback",
        ["user_id", "type", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_feedback_article",
        "feedback",
        ["article_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_feedback_article", table_name="feedback")
    op.drop_index("idx_feedback_user_type", table_name="feedback")
    op.drop_table("feedback")
