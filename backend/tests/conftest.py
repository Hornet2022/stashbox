"""pytest 公共 fixture：把仓库根父目录加入 sys.path（stashbox 包导入路径）。

运行方式（必须）：cd backend && pytest
  - backend 是 cwd 时 sys.path[0] 自动是 backend，所有 `from stashbox.backend...` 工作
  - 父目录 parents[3] 是兜底：cwd 不在 backend 时也能 import stashbox 包

⚠️ **测试必须打独立 DB / Redis —— 本模块负责在 import 任何业务代码之前切过去。**

没有这层隔离时，`pytest tests/` 会直接连 backend/.env 里的生产库和生产队列。
实测代价（2026-10-01）：一次全量 898 项的测试往生产库灌了 **547 条蒸馏记录**
（含 458 条 queued，会把本机 TTS 占满 20+ 小时），另外还造了 560 个假用户、
1110 篇 example.com 文章 —— 而且这些 queued 任务**会被 ai-worker 真的消费掉**，
不是躺在库里没人管。

隔离方式是在**模块顶层**改 os.environ，而不是 fixture：业务模块
（`common.config` / `common.database` / `common.redis_client`）都在 import 期就
读配置建连接池，fixture 跑起来时它们已经 import 过了。

优先级依据 pydantic-settings：init 参数 > os.environ > dotenv 文件，
所以 os.environ 覆盖 backend/.env。这里用 setdefault，显式设了的环境变量优先，
这样 CI 可以用别的方式接管。
"""

import os
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])  # .../work
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)


def _is_test_run() -> bool:
    """恒为 True —— conftest.py 只会被 pytest 收集时加载，没有别的入口。

    ⚠️ 之前这里判断的是 `"PYTEST_CURRENT_TEST" in os.environ`，**是错的**：
    那个变量只在测试**运行**阶段设置，collection 阶段（也就是 conftest 顶层
    执行的时候）还没设。于是判断为 False，隔离整段没生效 ——
    2026-10-01 复核时又往生产库写了 18 篇文章、7 条蒸馏记录才发现。
    """
    return True


# ⚠️ 必须在下面这些 import 之前设置，业务模块 import 期就会读走。
#   stashbox.backend.common.config      → pydantic Settings
#   stashbox.backend.common.database    → create_async_engine
#   stashbox.backend.common.redis_client→ redis 连接池
#   ai-service 的 arq 设置               → REDIS_URL
if _is_test_run():
    os.environ.setdefault("POSTGRES_DB", "stashbox_test")
    os.environ.setdefault("REDIS_DB", "15")
    # arq 不走 common.config，自己读 REDIS_URL，所以要单独覆盖成测试库。
    _db = os.environ["POSTGRES_DB"]
    _rdb = os.environ["REDIS_DB"]
    os.environ.setdefault("REDIS_URL", f"redis://localhost:6379/{_rdb}")
    # 让 arq 的队列名带 test 前缀，双保险：万一 REDIS_URL 没生效（比如别的进程
    # 用了生产 URL），ai-worker 也不会误消费测试入队的任务。
    os.environ.setdefault("ARQ_QUEUE_NAME", "stashbox:test:distill")


@pytest.fixture(scope="session", autouse=True)
async def _test_database_schema():
    """会话开始时把测试库的表建好。

    用 alembic 跑真实迁移而不是 metadata.create_all —— 迁移文件里可能带
    create_all 表达不了的语句（ALTER、索引 CONCURRENTLY 等），create_all 建出来的
    schema 会和生产不一致，测出来的东西没有意义。
    """
    if not _is_test_run():
        yield
        return

    import asyncio
    import subprocess
    from pathlib import Path as _Path

    backend_dir = _Path(__file__).resolve().parents[1]
    script = _Path(backend_dir) / "scripts" / "ensure_test_db.py"
    if not script.exists():
        pytest.skip(f"缺少 {script}，无法准备测试库")

    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(script)],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "准备测试库失败。\n"
            f"命令: {sys.executable} {script}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    yield


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
