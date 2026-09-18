"""收藏表（CP5.5）。"""
from sqlalchemy import BigInteger, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Favorite(Base, TimestampMixin):
    """收藏 + folder + note。"""

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    article_id: Mapped[str] = mapped_column(
        Text, ForeignKey("articles.id"), nullable=False
    )
    folder: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="default"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "article_id", "folder", name="uq_favorites_user_article_folder"),
        Index("idx_favorites_user_folder", "user_id", "folder"),
    )
