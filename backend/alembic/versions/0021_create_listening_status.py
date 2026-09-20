"""CP11.0.1 listening_status 表 — Android 断点续听

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listening_statuses",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.String(length=32), nullable=False),
        sa.Column("position_sec", sa.Integer(), nullable=False),
        sa.Column("total_sec", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.UniqueConstraint("user_id", "article_id", name="uq_listening_status_user_article"),
        sa.Index("idx_listening_status_user", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("listening_statuses")
