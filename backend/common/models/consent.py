"""CP5.6.0 §3.1：用户同意记录表（个性化 + 跨用户金句）。

按 docs/听感产品化方案_v1.md §3.1 严格实现：
- 个性化开关（GDPR 合规 + 隐私边界）
- 跨用户金句开关
- 同意版本号（隐私政策 v2）
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    String,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ConsentRecord(Base, TimestampMixin):
    """CP5.6.0 §3.1：用户同意记录。

    - 一对一：user_id 主键（unique）
    - 个性化开关：personalization_enabled（默认 False，opt-in）
    - 跨用户金句开关：cross_user_share_enabled（默认 False）
    - 同意时间 + 同意版本（隐私政策 v2）
    """

    __tablename__ = "user_consents"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cross_user_share_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consent_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v2")

    # TimestampMixin 字段重写（SQLite 兼容 + 显式声明）
    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)
