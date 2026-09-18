"""CP5.5 create favorites + later_listens

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # favorites
    op.create_table(
        "favorites",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("article_id", sa.String(32), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("folder", sa.Text(), nullable=False, server_default="default"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.UniqueConstraint("user_id", "article_id", "folder", name="uq_favorites_user_article_folder"),
    )
    op.create_index("idx_favorites_user_folder", "favorites", ["user_id", "folder"])

    # later_listens
    op.create_table(
        "later_listens",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("article_id", sa.String(32), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("snooze_until", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.UniqueConstraint("user_id", "article_id", name="uq_later_listens_user_article"),
    )
    op.create_index("idx_later_listens_user", "later_listens", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_later_listens_user", table_name="later_listens")
    op.drop_table("later_listens")
    op.drop_index("idx_favorites_user_folder", table_name="favorites")
    op.drop_table("favorites")
