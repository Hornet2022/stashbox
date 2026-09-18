"""文章表 - 用户加入的原始内容（待蒸馏）。"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Article(Base, TimestampMixin):
    """文章表（v1 §4.3.1）。

    favorite / skip 为布尔标记，保留 CP1.4 的收藏 / 跳过语义
    （v1 §4.3.1 仅列 status 字段，此处补齐以兼容既有端点）。
    """

    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # art_xxx
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(
        String(32), default="unknown", nullable=False
    )  # wechat/douyin/web/d9/clawbot/pdf
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending/distilling/ready/listened/failed
    raw_content: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )  # 抓取后的原始内容（L4 多模态理解输入）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )  # CP5.2 用户端重试计数

    # CP1.4 端点保留字段
    favorite: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    skip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("idx_articles_user_status", "user_id", "status"),
        Index("idx_articles_created", "created_at"),
    )
