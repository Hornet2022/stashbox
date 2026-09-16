"""pytest 公共 fixture：把仓库根父目录加入 sys.path（stashbox 包导入路径）。"""
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])  # .../work
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)


@pytest.fixture(autouse=True)
async def _dispose_pools():
    """每个 case 结束释放 DB/Redis 连接池。

    pytest-asyncio 为每个 async test 建新 event loop，而连接池是模块级全局的，
    不释放会把上一个 loop 的连接带到下一个 case（asyncpg: attached to a different loop）。
    """
    yield
    from stashbox.backend.common import database, redis_client

    await database.engine.dispose()
    pool = redis_client._redis_pool
    if pool is not None:
        await pool.disconnect(inuse_connections=True)
        redis_client._redis_pool = None
