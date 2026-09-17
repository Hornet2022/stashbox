"""Tag + TagSubscription model（CP5.3a）。v1 §11.5 CP5.3。"""
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from stashbox.backend.common.models.base import Base


class Tag(Base):
    """主题标签。"""
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, server_default="subject")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    creator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="NOW()", nullable=False
    )

    __table_args__ = (
        Index("idx_tags_slug", "slug", unique=True),
        Index("idx_tags_category", "category"),
    )


class TagSubscription(Base):
    """用户订阅标签。"""
    __tablename__ = "tag_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="NOW()", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "tag_id", name="uq_tag_subscription_user_tag"),
        Index("idx_tag_subs_user", "user_id"),
        Index("idx_tag_subs_tag", "tag_id"),
    )
