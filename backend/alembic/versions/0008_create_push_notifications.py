"""create push_notifications table（CP5.4a）。v1 §11.5 CP5.4。

客户端拉取此表获取推送。真推送（极光/友盟）CP4.6 范围。
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("distilled_articles.id", ondelete="CASCADE"), nullable=True),
        sa.Column("tag_slug", sa.String(64), sa.ForeignKey("tags.slug", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("deeplink", sa.String(256), nullable=True),  # 客户端点推送打开的 URL
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_push_notif_user", "push_notifications", ["user_id"])
    op.create_index("idx_push_notif_user_unread", "push_notifications", ["user_id", "read_at"])
    op.create_index("idx_push_notif_article", "push_notifications", ["article_id"])


def downgrade() -> None:
    op.drop_index("idx_push_notif_article", table_name="push_notifications")
    op.drop_index("idx_push_notif_user_unread", table_name="push_notifications")
    op.drop_index("idx_push_notif_user", table_name="push_notifications")
    op.drop_table("push_notifications")
