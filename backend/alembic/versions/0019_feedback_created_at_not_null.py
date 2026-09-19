"""CP7.3-AUDIT-DRIFT-7 feedback.created_at NOT NULL — 与模型对齐

common/models/feedback.py:25 声明 created_at nullable=False，
但 0004 建表时没带上，DB 里是 nullable=YES —— fresh 库/ CI 也会重现的
源码级漂移（本机 201 行埋点里 0 行 NULL，改 NOT NULL 不动数据）。

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "feedback",
        "created_at",
        existing_type=sa.TIMESTAMP(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "feedback",
        "created_at",
        existing_type=sa.TIMESTAMP(),
        nullable=True,
    )
