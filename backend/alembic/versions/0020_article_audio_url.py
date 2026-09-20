"""CP10 article audio_url 列 — 蒸馏完成后写回 audio_url 到 articles 表

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("audio_url", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("articles", "audio_url")
