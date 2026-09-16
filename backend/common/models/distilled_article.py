"""蒸馏结果表 - L4 蒸馏后的听感稿 + 音频。"""
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class DistilledArticle(Base, TimestampMixin):
    """蒸馏结果表（v1 §4.3.2）。

    status 字段用于蒸馏任务状态机（queued/running/done/failed），
    验收要求能从库里读到 status=done，故在 v1 基础上补齐。
    """

    __tablename__ = "distilled_articles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # dst_xxx
    article_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("articles.id"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="queued", nullable=False
    )  # queued/running/done/failed + CP3.5-pre-2 细粒度
    #   step1_structuring / step2_rewriting / step3_ttsing / step4_concatenating
    #   （CP3.5-pre-3 起由 Arq worker 写库，最长 19 字符，VARCHAR(16) 装不下，见 0003 迁移）
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 听感稿全文
    audio_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # OSS URL
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 音频时长（秒）
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 主题标签 ["科技","商业"]
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # LLM 自评 0-10

    __table_args__ = (
        Index("idx_distilled_article_id", "article_id"),
    )
