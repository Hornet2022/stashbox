"""PushNotification model（CP5.4a）。v1 §11.5 CP5.4。"""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from stashbox.backend.common.models.base import Base


class PushNotification(Base):
    """推送队列表（CP5.4a）。客户端拉取；真推送 CP4.6。"""
    __tablename__ = "push_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("distilled_articles.id", ondelete="CASCADE"), nullable=True
    )
    tag_slug: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("tags.slug", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deeplink: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="NOW()", nullable=False
    )

    __table_args__ = (
        Index("idx_push_notif_user", "user_id"),
        Index("idx_push_notif_user_unread", "user_id", "read_at"),
        Index("idx_push_notif_article", "article_id"),
    )
