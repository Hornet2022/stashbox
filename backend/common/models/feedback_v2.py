"""反馈分类表 v2（CP5.5-A3）。"""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class FeedbackV2(Base):
    """用户主动反馈（分类 + 可选评分）。

    与既有 feedback 表并存——feedback 是埋点事件，feedback_v2 是用户主动反馈。
    """

    __tablename__ = "feedback_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    article_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("articles.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)",
            name="ck_feedback_v2_rating_range",
        ),
        CheckConstraint(
            "category IN ('bug', 'feature', 'content', 'audio_quality', 'other')",
            name="ck_feedback_v2_category_enum",
        ),
        Index("idx_feedback_v2_user_category", "user_id", "category", "created_at"),
        Index("idx_feedback_v2_article", "article_id"),
    )
