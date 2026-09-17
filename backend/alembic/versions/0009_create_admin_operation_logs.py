"""create admin_operation_logs table（CP3.6-A1）。v1 §3.6 5 原则 2。

与用户 operation_logs 物理隔离。所有 admin/operator 写操作自动记录。
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_operation_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("admin_tier", sa.String(16), nullable=False),  # 冗余存 admin 当时的 tier（admin/operator）
        sa.Column("action", sa.String(64), nullable=False),  # quota_adjust / force_retry / audio_invalidate / ...
        sa.Column("target_type", sa.String(32), nullable=True),  # user / article / audio / ...
        sa.Column("target_id", sa.String(64), nullable=True),  # target 主键（int 或 str）
        sa.Column("reason", sa.Text(), nullable=False),  # v1 §3.6 5 原则 1「所有写操作必填 reason」
        sa.Column("method", sa.String(8), nullable=False),  # POST/PUT/DELETE/PATCH
        sa.Column("path", sa.String(256), nullable=False),  # /api/v1/admin/users/123/quota-adjust
        sa.Column("request_body", sa.JSON(), nullable=True),  # 请求体（脱敏后）
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),  # IPv4/IPv6
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_admin_oplog_admin", "admin_operation_logs", ["admin_id"])
    op.create_index("idx_admin_oplog_action", "admin_operation_logs", ["action"])
    op.create_index("idx_admin_oplog_created", "admin_operation_logs", ["created_at"])
    op.create_index("idx_admin_oplog_target", "admin_operation_logs", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("idx_admin_oplog_target", table_name="admin_operation_logs")
    op.drop_index("idx_admin_oplog_created", table_name="admin_operation_logs")
    op.drop_index("idx_admin_oplog_action", table_name="admin_operation_logs")
    op.drop_index("idx_admin_oplog_admin", table_name="admin_operation_logs")
    op.drop_table("admin_operation_logs")
