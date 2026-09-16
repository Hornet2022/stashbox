"""
数据库连接 - SQLAlchemy 2.0 async + asyncpg。
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from stashbox.backend.common.config import settings


class Base(DeclarativeBase):
    """所有 ORM model 的基类"""
    pass


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
