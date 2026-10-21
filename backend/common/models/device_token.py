"""设备令牌（推送投递的落点）（2026-10-21）

为什么这张表以前不存在
----------------------
`push_notification` 表记的是"**要推什么**"（user_id + title + body），
但**没有任何地方记"往哪台设备推"**：
- 没有 `device_token` 列
- 没有设备注册端点
- 没有投递 worker

所以推送这件事此前只有"落库"，没有"投递"。安卓端拉
`GET /api/v1/notifications` 能看到"有一条通知"，但手机上不会有任何东西。
管理后台那个「失败重发」更是直接把 `status` 改成 `'sent'` 而不发起任何投递
（user-service/main.py 的 admin_retry_push_notification）—— 运营看到"已发送"，
用户手机上什么都没有。

这张表是"真的把消息推到手机上"的前提。

字段说明
--------
- ``token``：厂商通道给的设备令牌。同一个用户在**不同通道**下有不同的 token，
  所以唯一约束是 (token, provider) 复合而不是单列。
- ``provider``：哪个通道的 token。值约定见 ``PushProvider.PROVIDERS``：
  ``none``（未接通道，仅登记）/ ``jpush`` / ``tpns`` / ``fcm`` / ``apns``。
- ``is_active``：解绑、登出、换手机都要能停用而不是删 —— 留一行记录，
  才知道"这台设备曾经有过推送"。
- ``deleted_at``：软删，与仓库其他业务表一致。

关于外键的一个坑
----------------
``user_id`` 是指向 ``users.id`` 的外键，**没有 ondelete=CASCADE**。
所以真要**硬删**一个用户行时，顺序必须是：

    先删 device_tokens → 再删 user_consents → 最后删 users

否则 PG 抛 ``ForeignKeyViolationError``（实测踩过一次）。
用户自助注销走的是**软删**（``users.deleted_at``），并且在删用户之前
已经把设备令牌软删、同意记录硬删，所以那条路径不受影响。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class DeviceToken(Base, TimestampMixin):
    """一台设备上的一个推送令牌。"""

    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="android")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("token", "provider", name="uq_device_tokens_token_provider"),
        Index(
            "idx_device_tokens_user_active",
            "user_id",
            postgresql_where="deleted_at IS NULL AND is_active",
        ),
    )

    # TimestampMixin 字段重写（SQLite 兼容 + 显式声明）
    created_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at = mapped_column(  # type: ignore[assignment]
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at = mapped_column(TIMESTAMP(timezone=False), nullable=True)
