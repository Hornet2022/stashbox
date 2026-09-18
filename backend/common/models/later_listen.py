"""稍后听表（CP5.5）。"""
from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class LaterListen(Base, TimestampMixin):
    """稍后听 + 可选 snooze_until。"""

    __tablename__ = "later_listens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    article_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("articles.id"), nullable=False
    )
    snooze_until: Mapped["TIMESTAMP | None"] = mapped_column(TIMESTAMP, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "article_id", name="uq_later_listens_user_article"),
        Index("idx_later_listens_user", "user_id"),
    )
