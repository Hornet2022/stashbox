"""收听进度表（CP11.0.1 Android 断点续听）。"""
from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ListeningStatus(Base, TimestampMixin):
    """用户对文章的收听进度（断点续听）。"""

    __tablename__ = "listening_statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(String(32), ForeignKey("articles.id"), nullable=False)
    position_sec: Mapped[int] = mapped_column(nullable=False, default=0)
    total_sec: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "article_id", name="uq_listening_status_user_article"),
        Index("idx_listening_status_user", "user_id"),
    )