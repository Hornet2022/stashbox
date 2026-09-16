"""所有 ORM 模型的基类。"""
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
