"""用户表 - 微信 / 手机号 / Apple 三种登录身份合并。"""

from sqlalchemy import BigInteger, Index, Integer, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from stashbox.backend.common.config import settings

from .base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """用户表（v1 §4.2.1）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    open_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    union_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    apple_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # CP3.6.2-XIN admin login：email 用于 admin 登录查用户；password_hash 存 bcrypt hash
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tier: Mapped[str] = mapped_column(
        String(16),
        default="free",
        nullable=False,
        doc="用户角色：free|student|member|pro|operator|admin（v1 §3.6，CP1.8 扩展 admin/operator）",
    )
    student_verified_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)
    student_expire_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)

    # CP1.6 配额字段（v1 §4.2.1 / §4.10 乐观锁扣减）
    # 2026-10-21：默认值改成读配置（STASHBOX_FREE_TIER_QUOTA）。
    #
    # 用 lambda 而不是写死数字 —— 写死的话，"内测期给多少配额"这件事
    # 就被焊死在模型定义里，改一次要重新发版。
    # default 是**调用时**求值的（SQLAlchemy 的 callable default），
    # 所以它读到的永远是进程当前生效的配置。
    #
    # server_default 仍是 "5"：那是 DDL 层的默认值，只在绕过 ORM 直接写库时
    # 才用得上（裸 SQL INSERT）。改它要动 migration，不值得为它单独发一版。
    monthly_quota: Mapped[int] = mapped_column(
        Integer,
        default=lambda: settings.free_tier_monthly_quota,
        server_default="5",
        nullable=False,
    )
    quota_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    quota_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )  # 乐观锁版本号
    quota_reset_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)

    # CP5.1 用户引导完成时间（v1 §11.5 首次打开转化率追踪）
    onboarding_done_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP, nullable=True)

    __table_args__ = (
        Index("idx_users_tier", "tier"),
        Index("idx_users_email", "email"),
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
