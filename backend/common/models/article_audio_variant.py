"""CP3.7.1：音频变体表（多码率支持，闭环 3 数据底座）。

每行 = 蒸馏文章的一个码率变体（128k / 96k / 64k）。
CP7.3.0 audio_variant_generator 异步生成多码率。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ArticleAudioVariant(Base, TimestampMixin):
    """CP3.7.1 §2.1.C：音频多码率变体。

    - unique(distilled_article_id, bitrate)：同一文章同一码率只 1 行
    - format 默认 'm4a'
    - sample_rate 24kHz / 16kHz（CP7.3.0 决策）
    - mono 默认 True（节省带宽）
    """

    __tablename__ = "article_audio_variants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # avar_<uuid24>
    distilled_article_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("distilled_articles.id"), nullable=False
    )
    bitrate: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 64 / 96 / 128 kbps
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    oss_key: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False, default="m4a")
    duration_sec: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sample_rate: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=24000)
    mono: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "distilled_article_id",
            "bitrate",
            name="idx_avar_task_bitrate",
        ),
        Index("idx_avar_bitrate_size", "bitrate", "file_size_bytes"),
    )
