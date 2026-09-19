"""SystemConfig model（CP7.3）。全局运行时配置的 key-value 表。

admin-web 改配置（LLM 服务商 / 模型 / API key）时写这里，
服务侧每次调用读一次 —— 改完即生效，不用重启。

约定：
- 每条记录是一组配置（如 key="llm" 的 value 是 LLM 配置 dict）
- 表里没写的字段回落到环境变量 / 代码默认值
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, BigInteger, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from stashbox.backend.common.models.base import Base


class SystemConfig(Base):
    """系统运行时配置（key-value，value 是 JSON dict）。"""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[Optional[int]] = mapped_column(
        # users.id 是 BigInteger，迁移 0016 也建的 BigInteger —— 这里保持一致
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="NOW()", nullable=False
    )
