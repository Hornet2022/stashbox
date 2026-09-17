"""AdminOperationLog model（CP3.6-A1）。v1 §3.6 5 原则 2。"""
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from stashbox.backend.common.models.base import Base


class AdminOperationLog(Base):
    """管理员操作日志（CP3.6-A1）。"""
    __tablename__ = "admin_operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    admin_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    request_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="NOW()", nullable=False
    )

    __table_args__ = (
        Index("idx_admin_oplog_admin", "admin_id"),
        Index("idx_admin_oplog_action", "action"),
        Index("idx_admin_oplog_created", "created_at"),
        Index("idx_admin_oplog_target", "target_type", "target_id"),
    )
