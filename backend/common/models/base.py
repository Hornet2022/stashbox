"""所有 ORM 模型的基类。

⚠️ TimestampMixin 覆盖现状（CP11.x 走查发现的不一致）：

  - User / Article / DistilledArticle 继承 TimestampMixin（已规范化）
  - Feedback / FeedbackV2 / PushNotification / AdminOperationLog 各自手写 created_at，
    列类型不统一：TIMESTAMP / DateTime(timezone=True) / postgresql.TIMESTAMP 三种

统一收编到 TimestampMixin 是 P1-3 目标，但**会引入 DDL 变更 + 数据迁移**：
  - 4 个表都要把 created_at 类型统一（推荐 TIMESTAMP）
  - updated_at / deleted_at 是否补齐需要业务侧决策（部分表无 update 语义、部分无软删除语义）

短期内（CP12 前）保持现状；P1-3 推迟到"DB 治理"专项任务（建议拆为独立 PR/CP，
避免与 P1-1~P1-5 的纯代码修复混在一起降低 review 难度）。
"""

from sqlalchemy import Column, TIMESTAMP, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类（全局唯一 metadata）。"""


class TimestampMixin:
    """created_at / updated_at / deleted_at 时间戳（UTC，软删除）。"""

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(TIMESTAMP, nullable=True)  # 软删除
