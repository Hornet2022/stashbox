"""CP3.7.1：few-shot 池（高分改写片段库，CP3.7.3 PostDistillHook 自动入选）。

每行 = 一条高分改写片段（hook / section / outro），供 Step 2 prompt 注入。
- user_id = NULL 表示全局池（任何用户都可复用）
- user_id != NULL 表示个人池（仅该用户复用）
- score_avg >= 4 才入选（CP3.7.3 严格门限）
- active = False 表示运营手动禁用（CP5.7 后续）
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import Float

from .base import Base, TimestampMixin


class FewShotExample(Base, TimestampMixin):
    """CP3.7.1 §2.1.D：few-shot 池条目。

    - source_pattern：语义指纹 hash（topic + rhythm + hook_type）
    - kind：'hook' / 'section' / 'outro'
    - source_eval_ids：来源 evaluation id 列表（JSON 字符串）
    - usage_count + last_used_at：池子运营 / 淘汰算法用
    """

    __tablename__ = "few_shot_examples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # fs_<uuid24>
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )  # NULL = 全局池
    source_pattern: Mapped[str] = mapped_column(String(64), nullable=False)
    rewrite_text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    score_avg: Mapped[float] = mapped_column(Float, nullable=False)
    source_eval_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array 字符串
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=False), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('hook', 'section', 'outro')",
            name="ck_fs_kind_enum",
        ),
        CheckConstraint(
            "score_avg BETWEEN 1 AND 5",
            name="ck_fs_score_range",
        ),
        Index("idx_fs_active_score", "active", "score_avg"),
        Index("idx_fs_user_kind", "user_id", "kind", "active"),
        Index("idx_fs_pattern", "source_pattern", "active"),
    )
