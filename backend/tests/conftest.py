"""pytest 公共 fixture：把仓库根父目录加入 sys.path（stashbox 包导入路径）。

运行方式（必须）：cd backend && pytest
  - backend 是 cwd 时 sys.path[0] 自动是 backend，所有 `from stashbox.backend...` 工作
  - 父目录 parents[3] 是兜底：cwd 不在 backend 时也能 import stashbox 包
"""

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

    try:
        await database.engine.dispose()
    except Exception:
        pass
    pool = redis_client._redis_pool
    if pool is not None:
        try:
            await pool.disconnect(inuse_connections=True)
        except Exception:
            pass
        redis_client._redis_pool = None
