"""CP5.5 create feedback_v2

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback_v2",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("article_id", sa.String(32), sa.ForeignKey("articles.id"), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("contact", sa.String(128), nullable=True),
        sa.Column("device_info", JSONB, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_feedback_v2_rating_range"),
        sa.CheckConstraint("category IN ('bug', 'feature', 'content', 'audio_quality', 'other')", name="ck_feedback_v2_category_enum"),
    )
    op.create_index("idx_feedback_v2_user_category", "feedback_v2", ["user_id", "category", "created_at"])
    op.create_index("idx_feedback_v2_article", "feedback_v2", ["article_id"])


def downgrade() -> None:
    op.drop_index("idx_feedback_v2_article", table_name="feedback_v2")
    op.drop_index("idx_feedback_v2_user_category", table_name="feedback_v2")
    op.drop_table("feedback_v2")
