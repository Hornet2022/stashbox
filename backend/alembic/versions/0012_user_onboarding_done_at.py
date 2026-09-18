"""CP5.1 user onboarding_done_at

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarding_done_at", sa.TIMESTAMP(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "onboarding_done_at")
