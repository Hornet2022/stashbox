"""feedback 表 - 埋点事件记录（v1 §4.3.4）。"""
from datetime import datetime

from sqlalchemy import BigInteger, Index, SmallInteger, String, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Feedback(Base):
    """v1 §4.3.4 反馈表（CP6.2.1）。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    article_id: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # CP6.2.1: 16 → 32
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # CP6.2.1: 32 → 64
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSON, nullable=True
    )  # 字段 metadata 跟 SQLAlchemy metadata 冲突，重命名
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_feedback_user_type", "user_id", "type", "created_at"),
        Index("idx_feedback_article", "article_id"),
    )
