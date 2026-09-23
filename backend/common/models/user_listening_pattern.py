"""CP3.7.1：用户听感画像（个性化改写的输入）。

每行 = 一个用户的听感偏好聚合（CP3.7.3 PostDistillHook 增量更新）。
feedback_count < 5 时所有画像字段保持 NULL（冷启动保护）。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    REAL,
    String,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class UserListeningPattern(Base, TimestampMixin):
    """CP3.7.1 §2.1.B：用户听感画像。

    - 主键 = user_id（一对一）
    - 30 篇滑动窗口 + 加权平均（新数据 0.7 / 历史 0.3）
    - 冷启动：feedback_count < 5 时所有画像字段 NULL
    """

    __tablename__ = "user_listening_patterns"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)

    feedback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_session_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skip_rate: Mapped[float | None] = mapped_column(REAL, nullable=True)
    completion_rate: Mapped[float | None] = mapped_column(REAL, nullable=True)
    preferred_rhythm: Mapped[str | None] = mapped_column(String(16), nullable=True)
    preferred_hook_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    avg_overall_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    last_distill_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_updated: Mapped[str | None] = mapped_column(
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)

    __table_args__ = (Index("idx_ulp_updated", "last_updated"),)
