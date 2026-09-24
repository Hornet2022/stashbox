"""CP5.4a-ADMIN：推送队列 admin 端点底座（2026-09-24）

背景：admin-web「推送队列」页(/push-notifications)需要看全量推送 + 按 status 排障。
  现状 push_notifications 只有 created_at / read_at，没有 status / error / sent_at。
  本迁移补三列供新端点 GET /api/v1/admin/push-notifications 使用。

补列：
  status VARCHAR(16) NOT NULL DEFAULT 'sent'
  error  TEXT NULL
  sent_at TIMESTAMP NULL

历史数据回填：
  status = 'sent'（已有行均已发出；无失败回执、无排队中间态）
  sent_at = created_at（与 status='sent' 自洽，避免 sent_at=NULL 但 status='sent' 的语义撕裂）

新增索引：
  idx_push_notif_status(status) —— 服务 admin 端点按 status 过滤

降级：删索引 + 删三列。

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 补 status 列（NOT NULL + DEFAULT 'sent'，历史行自动落 'sent'）
    op.add_column(
        "push_notifications",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="sent"),
    )
    # 2) 补 error 列（可空）
    op.add_column(
        "push_notifications",
        sa.Column("error", sa.Text(), nullable=True),
    )
    # 3) 补 sent_at 列（可空 TIMESTAMP，与 created_at 风格统一；无 tz）
    op.add_column(
        "push_notifications",
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=True),
    )
    # 4) 历史行 sent_at 回填为 created_at（保持与 status='sent' 自洽）
    op.execute("UPDATE push_notifications SET sent_at = created_at WHERE sent_at IS NULL")
    # 5) 索引
    op.create_index("idx_push_notif_status", "push_notifications", ["status"])


def downgrade() -> None:
    op.drop_index("idx_push_notif_status", table_name="push_notifications")
    op.drop_column("push_notifications", "sent_at")
    op.drop_column("push_notifications", "error")
    op.drop_column("push_notifications", "status")
