"""PushNotification model（CP5.4a）。v1 §11.5 CP5.4。

P1-3 修复（CP11.x 走查）：
  created_at 之前用 DateTime(timezone=True) + "NOW()"（手写字符串 default），
  与项目其他表的 TIMESTAMP + func.now() 风格不一致。统一改用 sqlalchemy.TIMESTAMP
  + func.now()，与其他 model 一致（底层 PG 都是 timestamptz，no-op DDL）。

  read_at 保持 DateTime(timezone=True)（业务侧明确需要带时区信息）。
  P1-3 只修 created_at 风格，不引入 updated_at / deleted_at（业务无需求）。

CP5.4a-ADMIN（推送队列 admin 端点 v1,2026-09-24）：
  补 status / error / sent_at 三列供 admin 全量排障使用。
  - status 枚举 'pending' / 'sent' / 'failed'，默认 'sent'（历史行已发出,无失败回执；
    老数据回填同样为 'sent'，避免出现 status=NULL 的诡异语义行）。
  - error 文本可空，status='failed' 时填具体原因。
  - sent_at 时间戳（无 tz，与 created_at 风格统一），status='sent' 时回填为 created_at，
    'pending' 行 NULL（待发），'failed' 行 NULL（未发出，无意义）。
  新增 idx_push_notif_status 索引,服务 admin 端点按 status 过滤。
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, Index, TIMESTAMP, func
from sqlalchemy.orm import Mapped, mapped_column
from stashbox.backend.common.models.base import Base


class PushNotification(Base):
    """推送队列表（CP5.4a）。客户端拉取；真推送 CP4.6。"""

    __tablename__ = "push_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("articles.id", ondelete="CASCADE"), nullable=True
    )
    tag_slug: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("tags.slug", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deeplink: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False
    )
    # CP5.4a-ADMIN：推送状态机三件套。默认 'sent' 与历史行回填保持一致，
    # 不引入 status=NULL 这种"未初始化"语义——业务侧没有这种状态。
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="sent")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)

    __table_args__ = (
        Index("idx_push_notif_user", "user_id"),
        Index("idx_push_notif_user_unread", "user_id", "read_at"),
        Index("idx_push_notif_article", "article_id"),
        # admin 端点按 status 过滤走索引；按 tag_slug 过滤量小可走 seq scan,
        # 不另开复合索引以免写放大。
        Index("idx_push_notif_status", "status"),
    )
