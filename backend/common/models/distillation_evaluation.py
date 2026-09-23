"""CP3.7.1：蒸馏听感评分表（4 维评分）。

每行 = 一次听感评分（用户在 Android 客户端提交 4 维评分）。
overall_score <= 2 → 触发自动重蒸（CP3.7.3）。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class DistillationEvaluation(Base, TimestampMixin):
    """CP3.7.1 §2.1.A：4 维听感评分。

    - hook_score / section_score / outro_score / rhythm_score 各自 1-5 分（nullable = 用户跳过该项）
    - overall_score = 4 维均值（1-5，**必填**，触发重蒸用）
    - skip_reason：用户在 Android 端选的跳过原因（CP3.7.0 同步）
    - auto_flag + retried_task_id：自动重蒸追踪
    """

    __tablename__ = "distillation_evaluations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # eval_<uuid24>
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("distilled_articles.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    hook_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    section_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    outro_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    rhythm_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    overall_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    auto_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # CP3.7.3 自动重蒸触发标志
    retried_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)

    __table_args__ = (
        # 业务约束（应用层校验 + DB 兜底）
        CheckConstraint(
            "overall_score BETWEEN 1 AND 5",
            name="ck_eval_overall_score_range",
        ),
        CheckConstraint(
            "(hook_score IS NULL OR hook_score BETWEEN 1 AND 5)",
            name="ck_eval_hook_score_range",
        ),
        CheckConstraint(
            "(section_score IS NULL OR section_score BETWEEN 1 AND 5)",
            name="ck_eval_section_score_range",
        ),
        CheckConstraint(
            "(outro_score IS NULL OR outro_score BETWEEN 1 AND 5)",
            name="ck_eval_outro_score_range",
        ),
        CheckConstraint(
            "(rhythm_score IS NULL OR rhythm_score BETWEEN 1 AND 5)",
            name="ck_eval_rhythm_score_range",
        ),
        Index("idx_eval_task", "task_id"),
        Index("idx_eval_user_score", "user_id", "overall_score"),
    )
