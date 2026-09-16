"""DistillDispatcher 单测（任务包 §4.1）。

不连真 Redis：把 dispatcher.create_pool 换成假的 pool，只验证入队参数 / 幂等 / 失败传播。
"""
import pytest

import dispatcher as dispatcher_module
from dispatcher import DistillDispatcher, get_dispatcher, shutdown_dispatcher


class FakeJob:
    def __init__(self, job_id: str = "job-abc123"):
        self.job_id = job_id


class FakePool:
    """记录 enqueue_job 调用 + close 次数，不做真实 Redis IO。"""

    def __init__(self, *, job: FakeJob | None = None):
        self.enqueued: list[tuple[str, dict]] = []
        self.closed = 0
        self._job = job if job is not None else FakeJob()

    async def enqueue_job(self, function: str, **kwargs):
        self.enqueued.append((function, kwargs))
        return self._job

    async def close(self):
        self.closed += 1


@pytest.fixture
def fake_pool(monkeypatch):
    """换掉 create_pool，返回 (pool, create_pool 的调用记录)。"""
    pool = FakePool()
    calls: list[dict] = []

    async def _create_pool(settings, **kwargs):
        calls.append({"settings": settings, **kwargs})
        return pool

    monkeypatch.setattr(dispatcher_module, "create_pool", _create_pool)
    return pool, calls


@pytest.fixture(autouse=True)
async def _reset_global_dispatcher():
    """每个 case 前后都清掉全局单例，避免 case 之间串味。"""
    await shutdown_dispatcher()
    yield
    await shutdown_dispatcher()


# ---------------------------------------------------------------------------
# 入队
# ---------------------------------------------------------------------------
async def test_enqueue_distill_returns_job_id(fake_pool):
    pool, _ = fake_pool

    job_id = await DistillDispatcher().enqueue_distill(
        task_id="dst_1",
        article_id="art_1",
        user_id=7,
        url="https://mp.weixin.qq.com/s/x",
        title="标题",
    )

    assert job_id == "job-abc123"


async def test_enqueue_distill_passes_all_args_to_arq(fake_pool):
    pool, _ = fake_pool

    await DistillDispatcher().enqueue_distill(
        task_id="dst_1",
        article_id="art_1",
        user_id=7,
        url="https://mp.weixin.qq.com/s/x",
        title="标题",
        simulate_failure=True,
    )

    function, kwargs = pool.enqueued[0]
    assert function == "distill_task"
    assert kwargs == {
        "task_id": "dst_1",
        "article_id": "art_1",
        "user_id": 7,
        "url": "https://mp.weixin.qq.com/s/x",
        "title": "标题",
        "simulate_failure": True,
    }


async def test_enqueue_distill_uses_configured_queue_name(fake_pool):
    """dispatcher 必须把队列名传给 Arq，否则 worker（读 WorkerSettings）收不到任务。"""
    _, calls = fake_pool

    await DistillDispatcher().enqueue_distill("dst_1", "art_1", 1, "u")

    assert calls[0]["default_queue_name"] == "stashbox:distill"


async def test_enqueue_distill_connects_lazily(fake_pool):
    pool, calls = fake_pool
    d = DistillDispatcher()

    assert d._pool is None  # 构造时不连 Redis
    await d.enqueue_distill("dst_1", "art_1", 1, "u")

    assert d._pool is pool
    assert len(calls) == 1


async def test_enqueue_distill_returns_empty_string_when_job_exists(fake_pool, monkeypatch):
    """Arq 在 job_id 重复时返 None（幂等去重），这里退化成空串而不是崩。"""
    pool, _ = fake_pool
    pool._job = None

    job_id = await DistillDispatcher().enqueue_distill("dst_1", "art_1", 1, "u")

    assert job_id == ""


# ---------------------------------------------------------------------------
# connect / close 幂等
# ---------------------------------------------------------------------------
async def test_connect_is_idempotent(fake_pool):
    pool, calls = fake_pool
    d = DistillDispatcher()

    await d.connect()
    await d.connect()
    await d.connect()

    assert len(calls) == 1
    assert d._pool is pool


async def test_close_is_idempotent(fake_pool):
    pool, _ = fake_pool
    d = DistillDispatcher()
    await d.connect()

    await d.close()
    await d.close()

    assert pool.closed == 1
    assert d._pool is None  # 关完可重连


async def test_close_without_connect_is_noop(fake_pool):
    pool, calls = fake_pool

    await DistillDispatcher().close()

    assert calls == []
    assert pool.closed == 0


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------
async def test_enqueue_raises_when_redis_is_down(monkeypatch):
    """Redis 连不上 → 异常往上抛（端点据此降级，见任务包 §8）。"""
    async def _boom(settings, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(dispatcher_module, "create_pool", _boom)

    with pytest.raises(ConnectionError, match="redis down"):
        await DistillDispatcher().enqueue_distill("dst_1", "art_1", 1, "u")


async def test_enqueue_raises_when_pool_enqueue_fails(fake_pool):
    pool, _ = fake_pool

    async def _boom(function, **kwargs):
        raise ConnectionError("connection lost")

    pool.enqueue_job = _boom

    with pytest.raises(ConnectionError, match="connection lost"):
        await DistillDispatcher().enqueue_distill("dst_1", "art_1", 1, "u")


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
async def test_get_dispatcher_returns_singleton():
    assert get_dispatcher() is get_dispatcher()


async def test_shutdown_dispatcher_resets_singleton(fake_pool):
    d = get_dispatcher()
    await d.connect()

    await shutdown_dispatcher()

    assert get_dispatcher() is not d
    assert d._pool is None


async def test_shutdown_dispatcher_is_safe_when_never_used():
    await shutdown_dispatcher()  # 不该抛
