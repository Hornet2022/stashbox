"""CP-AGENT-MEMORY-FEW-SHOT-USAGE：few-shot 取用记账（2026-10-02 端到端自测新增）。

## 背景：指标在骗人

`usage_count` 恒为 0，而 admin 后台（`ai-service/admin_router.py:105,569`）
把它**直接展示给运营看**。于是池子里唯一那条样本一直显示「从未被使用」。

根因是记账逻辑挂错了地方：

- `few_shot_pool.select_few_shot` 确实会累加 `usage_count`
- 但它只被 `hooks_impl.FewShotSelectorHook` 调用，而那是**死代码**
  （生产蒸馏走 LangGraph agent，hooks 根本不执行）
- 生产真正跑的是 `agent.memory.load_few_shots`，它只 SELECT 不记账

池健康度、LRU 择取排序（`few_shot_pool.py:215` 按 `usage_count DESC`）
全都建立在这个恒 0 的列上。

## 特别防护的一点

记账必须开**自己的 session**。曾经的写法把 SELECT 那个
`async with self._session_factory() as db` 里的 `db` 拿到块外执行 UPDATE ——
那个 session 早已出栈关闭，execute 复用了连接池里仍持锁未提交的连接，
第二次调用永久阻塞在 `Lock ... transactionid`。所以这里对
「连续调用不挂起」加了 `asyncio.wait_for` 超时断言。
"""

import asyncio
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

AI_SERVICE_DIR = str(Path(__file__).resolve().parents[2] / "ai-service")
if AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, AI_SERVICE_DIR)

# 「不该挂起」的上限。真库单次往返 ~30ms，留足余量又不至于掩盖真死锁。
NO_HANG_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def few_shot_db():
    """SQLite 内存库 + 一条 few-shot 样本。"""
    from datetime import datetime

    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # 生产代码用 `func.now()` → 编译成 `NOW()`，SQLite 不认这个函数。
    # 在连接层注册它，而不是把生产代码改成 CURRENT_TIMESTAMP 去迁就测试。
    @event.listens_for(engine.sync_engine, "connect")
    def _register_now(dbapi_conn, _rec):  # pragma: no cover - 连接钩子
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE few_shot_examples ("
                "  id VARCHAR PRIMARY KEY,"
                "  user_id VARCHAR,"
                "  source_pattern VARCHAR,"
                "  rewrite_text VARCHAR,"
                "  kind VARCHAR,"
                "  score_avg FLOAT,"
                "  usage_count INTEGER,"
                "  active BOOLEAN,"
                "  created_at TIMESTAMP,"
                "  updated_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO few_shot_examples "
                "(id, user_id, source_pattern, rewrite_text, kind, score_avg, usage_count, active, created_at, updated_at) "
                "VALUES ('fs_a', NULL, 'pat', '一个真正的高分开场白', 'hook', 4.0, 0, 1, NOW(), NOW())"
            )
        )
    yield Session
    await engine.dispose()


def _make_store(Session):
    from agent.memory import MemoryStore

    return MemoryStore(session_factory=Session)


async def _usage(Session, row_id="fs_a"):
    from sqlalchemy import text

    async with Session() as db:
        r = await db.execute(
            text("SELECT usage_count FROM few_shot_examples WHERE id = :i"), {"i": row_id}
        )
        return r.scalar()


# ---------------------------------------------------------------------------
# 1. 记账生效
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_few_shots_increments_usage_count(few_shot_db):
    store = _make_store(few_shot_db)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert await _usage(few_shot_db) == 1


@pytest.mark.asyncio
async def test_repeated_calls_accumulate(few_shot_db):
    store = _make_store(few_shot_db)
    for _ in range(3):
        # 每次新 store 绕开 300s 缓存，模拟三次独立蒸馏
        store = _make_store(few_shot_db)
        await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert await _usage(few_shot_db) == 3


@pytest.mark.asyncio
async def test_null_usage_count_becomes_one(few_shot_db):
    """`NULL + 1` 仍是 NULL —— 列允许 NULL，必须 coalesce。"""
    from sqlalchemy import text

    async with few_shot_db() as db:
        await db.execute(text("UPDATE few_shot_examples SET usage_count = NULL"))
        await db.commit()

    store = _make_store(few_shot_db)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert await _usage(few_shot_db) == 1


@pytest.mark.asyncio
async def test_updated_at_is_touched(few_shot_db):
    from sqlalchemy import text

    async with few_shot_db() as db:
        await db.execute(text("UPDATE few_shot_examples SET updated_at = '2000-01-01 00:00:00'"))
        await db.commit()

    store = _make_store(few_shot_db)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    async with few_shot_db() as db:
        r = await db.execute(text("SELECT updated_at FROM few_shot_examples WHERE id='fs_a'"))
        assert str(r.scalar())[:4] != "2000"


# ---------------------------------------------------------------------------
# 2. 回归防护：连续调用不得挂起
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_calls_do_not_hang(few_shot_db):
    """**这是最重要的一条**。

    曾经的实现把 SELECT 那个 `async with` 块里的 db 拿到块外执行 UPDATE，
    复用了连接池里仍持锁的连接 → 第二次调用永久阻塞。这里用 wait_for
    把「静默挂起」变成一个响亮的失败。
    """
    for i in range(4):
        store = _make_store(few_shot_db)
        try:
            await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)
        except asyncio.TimeoutError:
            pytest.fail(
                f"第 {i + 1} 次 load_few_shots 挂起了 —— 记账很可能又用到了"
                " 已关闭的 session（会造成 transactionid 行锁自阻塞）"
            )

    assert await _usage(few_shot_db) == 4


@pytest.mark.asyncio
async def test_bump_uses_its_own_session(few_shot_db):
    """_bump_usage 不接受外部 session —— 签名本身就是防护。"""
    import inspect

    from agent.memory import MemoryStore

    params = list(inspect.signature(MemoryStore._bump_usage).parameters)
    assert params == ["self", "ids"], (
        f"_bump_usage 签名变了：{params}。"
        "它必须自己开 session；一旦接受外部 db 就有复用已关闭 session 的风险"
    )


# ---------------------------------------------------------------------------
# 3. 缓存语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_does_not_double_count(few_shot_db):
    """同一 store 连续调用命中 300s 缓存 → 只记一次。

    否则一篇长稿切十几段会把同一条样本刷成十几次「使用」。
    """
    store = _make_store(few_shot_db)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)
    await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert await _usage(few_shot_db) == 1


# ---------------------------------------------------------------------------
# 4. 失败兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_bump_failure_does_not_break_load(few_shot_db, monkeypatch):
    """记账失败必须 best-effort —— 池子统计不该拖垮蒸馏。"""
    store = _make_store(few_shot_db)

    async def boom(ids):
        raise RuntimeError("模拟记账失败")

    monkeypatch.setattr(store, "_bump_usage", boom)

    shots = await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert len(shots) == 1, "记账炸了也不能影响 few-shot 取用"
    assert shots[0].output_excerpt == "一个真正的高分开场白"


@pytest.mark.asyncio
async def test_empty_table_returns_empty_and_does_not_hang(few_shot_db):
    from sqlalchemy import text

    async with few_shot_db() as db:
        await db.execute(text("DELETE FROM few_shot_examples"))
        await db.commit()

    store = _make_store(few_shot_db)
    shots = await asyncio.wait_for(store.load_few_shots(limit=5), timeout=NO_HANG_TIMEOUT)

    assert shots == []


@pytest.mark.asyncio
async def test_bump_with_no_ids_is_noop(few_shot_db):
    store = _make_store(few_shot_db)
    await asyncio.wait_for(store._bump_usage([]), timeout=NO_HANG_TIMEOUT)
    # 没炸就算过（空列表直接 return）
