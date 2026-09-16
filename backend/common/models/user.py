"""用户表 - 微信 / 手机号 / Apple 三种登录身份合并。"""
from sqlalchemy import BigInteger, Index, Integer, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """用户表（v1 §4.2.1）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    open_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    union_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    apple_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tier: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    student_verified_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)
    student_expire_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)

    # CP1.6 配额字段（v1 §4.2.1 / §4.10 乐观锁扣减）
    monthly_quota: Mapped[int] = mapped_column(
        Integer, default=5, server_default="5", nullable=False
    )
    quota_used: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    quota_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )  # 乐观锁版本号
    quota_reset_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)

    __table_args__ = (
        Index("idx_users_tier", "tier"),
        Index(
            "idx_users_open_id_active",
            "open_id",
            postgresql_where="deleted_at IS NULL",
        ),
        Index(
            "idx_users_phone_active",
            "phone",
            postgresql_where="deleted_at IS NULL",
        ),
    )
