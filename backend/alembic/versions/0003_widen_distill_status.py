"""widen distilled_articles.status: VARCHAR(16) → VARCHAR(32)

CP3.5-pre-3：蒸馏任务改由 Arq worker 跑 CP3.5-pre-2 的 4 步流水线后，
status 写的是细粒度状态（step1_structuring / step2_rewriting / step3_ttsing /
step4_concatenating），最长的 step4_concatenating 有 19 个字符，原来的
VARCHAR(16) 装不下 —— PG 会抛 StringDataRightTruncationError，任务永远失败。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "distilled_articles",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "distilled_articles",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
