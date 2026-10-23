"""蒸馏结果表 - L4 蒸馏后的听感稿 + 音频。"""

from sqlalchemy import (
    Boolean,
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
    audio_url: Mapped[str | None] = mapped_column(String(512), nullable=True)  # OSS URL
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 音频时长（秒）
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 主题标签 ["科技","商业"]
    # 2026-10-23：**三类再创作内容分区**。
    #
    # {"sections": [...], "insights": [...], "related": [...], "follow_ups": [...]}
    #
    # 为什么单独一列：这三类在改写 JSON 里是三个独立字段，但解析时被
    # **合并进 sections**（TTS 整稿念的那一份）。合并之后下游**彻底分不清**
    # 「关联思考」和「正文要点」—— API 里只有一坨 `script_text`，
    # App 端也没法分区展示（等于这三类一直在生成、用户却永远看不到）。
    #
    # 存**一列 JSONB** 而不是六个 Text 列：这三类天然是一组，
    # 一起读写、一起版本演进；拆成六列会让每次加字段都要写迁移。
    #
    # NULL = 0039 之前的历史数据（没有分区信息）→ 读取方按「空分区」处理。
    content_parts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # LLM 自评 0-10
    # B4（修 D1）：A/B 实验分组落库。NULL = 0029 上线前的历史数据（未实验期），
    # ab-report 按 NULL 单独一组过滤。方案 §2.7-D：user_id % 100 < 30 → personalized。
    ab_group: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_personalized: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # CP-TTS-VOICE：这段音频当时用哪个音色合成的（溯源）。
    # **刻意可空**：0033 之前跑出来的历史行无从得知音色，留 NULL 表示
    # 「未知 / 当时用的是全局默认参考音频」，比回填一个假音色诚实。
    # 音色被删时 SET NULL —— 音频本身还在，只是失去了溯源信息。
    tts_voice_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_distilled_article_id", "article_id"),
        Index("idx_distilled_tts_voice", "tts_voice_id"),
        # 0035：后台统计面板全是 `status = ? AND updated_at > ?` 的形状
        # （失败数、近 7 天趋势、按天聚合）。
        Index("idx_distilled_status_updated", "status", "updated_at"),
        # 0035：按标签筛选走 JSONB 包含运算符 `tags.contains([name])`（底层 `@>`），
        # 没有 GIN 就是整表顺序扫 + 逐行 JSON 解析。用默认 jsonb_ops。
        Index("idx_distilled_tags_gin", "tags", postgresql_using="gin"),
    )
