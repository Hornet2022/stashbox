"""
数据库连接 - SQLAlchemy 2.0 async + asyncpg。
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 全局唯一 Base / metadata 来自 common.models.base，避免与 ORM 模型出现双 metadata。

from stashbox.backend.common.config import settings


# === 全局 engine（一个进程一个） ===
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.postgres_pool_size,
    pool_recycle=settings.postgres_pool_recycle,
    echo=settings.debug,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入 - 每个请求一个 session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
