"""所有 ORM 模型的基类。

P1-3 修复后（CP11.x 走查）：
  - User / Article / DistilledArticle 继承 TimestampMixin（已规范化）
  - Feedback / FeedbackV2 / PushNotification / AdminOperationLog 各自手写 created_at，
    统一用 sqlalchemy.TIMESTAMP + func.now()（与 TimestampMixin 的 created_at 列
    完全等价；只继承 created_at 字段，不引入 updated_at / deleted_at —— 业务侧无需求）

  - alembic 0023_unify_created_at_type 已将 push_notifications /
    admin_operation_logs 的 created_at 显式 alter 到 timestamp without time zone，
    与 ORM 声明对齐。

  对 PushNotification / AdminOperationLog 不继承 TimestampMixin 的设计说明：
    - 这两张表是 append-only 日志（推送队列 / 审计日志），updated_at 会让
      「CP3.6-A1 合规审计 / 推送重试状态」语义模糊
    - audit log 软删除（deleted_at）会破坏合规要求（监管要求保留 N 年、不可删）
    - 若日后需要 audit log 的 deleted_at 用于「运营可撤销」（合规允许范围内），
      再单独建一张 AdminOperationLogArchive 表而非在本表加 deleted_at
"""

from sqlalchemy import Column, TIMESTAMP, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类（全局唯一 metadata）。"""


class TimestampMixin:
    """created_at / updated_at / deleted_at 时间戳（UTC，软删除）。

    仅适用于业务实体表（User / Article / DistilledArticle）；日志类表
    （Feedback / FeedbackV2 / PushNotification / AdminOperationLog）按业务
    侧选择只继承 created_at 风格、不引 updated_at/deleted_at。
    """

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(TIMESTAMP, nullable=True)  # 软删除
