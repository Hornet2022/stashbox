"""CP2.7 集成测 fixtures：db_setup / redis_setup / fetch_recorder / ai_client_stub。

导入说明（同 backend/content-service/tests/test_wechat_mp_handler.py）：
content-service 目录名带连字符，不能当包 import，只能按文件路径加载 main.py；
而 tests/ 被 pytest 塞进 sys.path 后，tests/fetchers/ 会和本服务的 fetchers
包同名 —— 故先把真包以 `_cp27_fetchers_pkg` 别名加载并临时挂到 "fetchers"
名下，main.py 加载完再还原。

DB 隔离：Handler 走自己的 session 并 commit，测试侧 rollback 挡不住，
所以每个 case 结束显式删掉本 case 期间新增的 `source='wechat_mp'` 行
（`db_setup` 按 created_at 时间窗删，只碰本 case 建的行）。

真实网络失败（超时 / 5xx / 限速）由用例 pytest.skip，不判 fail。
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from redis import asyncio as aioredis
from sqlalchemy import delete, text

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
# `stashbox` 包从仓库父目录解析（本地跑时 pytest 不一定带 PYTHONPATH）
REPO_PARENT = str(CONTENT_SERVICE_DIR.parent.parent)
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

TEST_SOURCE = "wechat_mp"  # 本集成测在 articles 表里的"命名空间"


def _load_module(name: str, path: Path, submodule_search_locations=None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=submodule_search_locations
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cs_fetchers = _load_module(
    "_cp27_fetchers_pkg",
    CONTENT_SERVICE_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_SERVICE_DIR / "fetchers")],
)
_shadowed = sys.modules.get("fetchers")
sys.modules["fetchers"] = cs_fetchers
try:
    content_main = _load_module("_cp27_content_main", CONTENT_SERVICE_DIR / "main.py")
finally:
    if _shadowed is None:
        del sys.modules["fetchers"]
    else:
        sys.modules["fetchers"] = _shadowed

from stashbox.backend.common.database import AsyncSessionLocal  # noqa: E402
from stashbox.backend.common.models import Article  # noqa: E402
from stashbox.backend.common.redis_client import get_redis_pool  # noqa: E402


class FakeAIClient:
    """ai_client 替身：不真发 HTTP 给 ai-service（避免污染 distilled_articles）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def trigger_distill(self, article_id: str, auth_token: str | None = None, **_kw):
        self.calls.append({"article_id": article_id, "auth_token": auth_token})
        return {
            "article_id": article_id,
            "task_id": f"dst_{uuid.uuid4().hex[:24]}",
            "status": "started",
        }


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


@pytest.fixture
async def db_setup():
    """真 DB session（PostgreSQL 5432）。

    yield 给用例查 articles；case 结束删掉本 case 期间新增的 wechat_mp 行。
    """
    started_at = datetime.utcnow() - timedelta(seconds=1)  # created_at 是 naive TIMESTAMP
    session = AsyncSessionLocal()
    try:
        await session.execute(text("SELECT 1"))  # PG 不在跑就直接报错（不停跑）
        yield session
    finally:
        try:
            await session.execute(
                delete(Article).where(
                    Article.source == TEST_SOURCE,
                    Article.created_at >= started_at,
                )
            )
            await session.commit()
        finally:
            await session.close()


@pytest.fixture
async def redis_setup():
    """Redis 6379 探活（cache_service 建文章时会失效待听缓存）。"""
    client = aioredis.Redis(connection_pool=get_redis_pool())
    try:
        await client.ping()
    except Exception as exc:  # Redis 不在跑 → 跳过（不是抓取层的问题）
        await client.aclose()
        pytest.skip(f"redis unavailable: {exc}")
        return
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def content_app():
    """被测 FastAPI app（content-service main.py，按文件路径加载）。"""
    return content_main.app


@pytest.fixture
def ai_client_stub(monkeypatch):
    """把 Handler 里的 get_ai_client 换成替身，避免真触发 ai-service 蒸馏。"""
    fake = FakeAIClient()
    monkeypatch.setattr(content_main, "get_ai_client", lambda: fake)
    return fake


@pytest.fixture
def fetch_recorder(monkeypatch):
    """包一层 GenericURLFetcher.fetch，把 FetchResult 记下来。

    只记录不改行为（返回值原样透传）—— 报告需要 content_text 长度，
    而 Handler 目前只把 title 落库，正文没进 articles。
    """
    captured: dict[str, object] = {}
    original = cs_fetchers.GenericURLFetcher.fetch

    async def wrapped(self, url: str, **kwargs):
        result = await original(self, url, **kwargs)
        captured["result"] = result
        return result

    monkeypatch.setattr(cs_fetchers.GenericURLFetcher, "fetch", wrapped)
    return captured
