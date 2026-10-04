"""CP-AGENT-QUOTA-REFUND：蒸馏失败退款的幂等与降级（2026-10-03 补）。

## 为什么之前没有覆盖

退款逻辑原先**没有任何有效测试**：要触发它得让蒸馏真跑到 failed，而生产蒸馏
走 LangGraph agent + 真实 TTS 队列，单篇约 12 分钟（实测 TTS 本身 745s/篇）。
于是 `backend/tests/test_quota.py` 里那条退款用例只能 `pytest.skip` 掉，留下一个
docstring 记录的缺口。

但其实**不需要跑真蒸馏**：`_refund_quota_once` 的两个依赖（Redis 幂等锁、
`quota_service.refund`）都是可替换的，monkeypatch 掉就能直接测。

## 锁的语义

Arq 默认 `retry_max=2`，同一个任务可能跑「首跑 + 2 次重试」。退款必须**只发生
一次**，否则用户被白退 2~3 次配额（= 白赚）。靠 Redis `SETNX refund:{task_id}`
加锁，TTL 24h 覆盖当天所有重试。
"""

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


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """够用的 Redis 替身：set(nx=True) / delete / aclose。"""

    def __init__(self, *, available: bool = True):
        self._available = available
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, bool]] = []
        self.deleted: list[str] = []

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, nx))
        if not self._available:
            raise ConnectionError("模拟 Redis 不可用")
        if nx and key in self.store:
            return None  # 没抢到锁
        self.store[key] = value
        return True

    async def delete(self, key):
        self.deleted.append(key)
        return 1 if self.store.pop(key, None) is not None else 0

    async def aclose(self):
        return None


class FakeSessionFactory:
    """AsyncSessionLocal 的替身：记录开了几次 session。"""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def refund_env(monkeypatch):
    """把 `_refund_quota_once` 的两个外部依赖换成可控替身。

    ⚠️ 2026-10-03 踩过的坑：早先这里 monkeypatch 的是全局 `redis.asyncio.Redis`，
    结果 `cache_service` 缓存的 client 变成了这个 FakeRedis，**污染到别的测试**，
    一次全量跑出 57 failed（`'FakeRedis' object has no attribute 'aclose'`）。

    现在只 patch `distill_task._make_refund_lock_client` 这一个 seam
    （生产代码为此专门抽了这个函数），redis 模块本身完全不动。
    """
    from tasks import distill_task
    from stashbox.backend.common import quota_service

    fake_redis = FakeRedis()
    sessions = FakeSessionFactory()
    refunded: list[int] = []

    async def fake_refund(session, user_id):
        refunded.append(user_id)

    async def fake_make_client():
        return fake_redis

    monkeypatch.setattr(quota_service, "refund", fake_refund)
    monkeypatch.setattr(distill_task, "AsyncSessionLocal", sessions, raising=False)
    monkeypatch.setattr(distill_task, "_make_refund_lock_client", fake_make_client)

    return type(
        "Env",
        (),
        {"redis": fake_redis, "sessions": sessions, "refunded": refunded},
    )()


# ---------------------------------------------------------------------------
# 1. 基本路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_首次调用会退一次配额(refund_env):
    from tasks.distill_task import _refund_quota_once

    await _refund_quota_once(task_id="dst_1", user_id=42)

    assert refund_env.refunded == [42]


@pytest.mark.asyncio
async def test_用_task_id_做锁键(refund_env):
    """锁键必须按 task 维度，而不是 user —— 同一用户可能有多个任务在跑。"""
    from tasks.distill_task import _refund_quota_once

    await _refund_quota_once(task_id="dst_abc", user_id=42)

    keys = [k for k, _ in refund_env.redis.set_calls]
    assert keys == ["refund:dst_abc"]


@pytest.mark.asyncio
async def test_锁用_nx_保证原子(refund_env):
    from tasks.distill_task import _refund_quota_once

    await _refund_quota_once(task_id="dst_1", user_id=42)

    assert all(nx for _, nx in refund_env.redis.set_calls), "必须用 SETNX，否则并发下会重复退款"


# ---------------------------------------------------------------------------
# 2. 幂等（Arq retry 场景）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_重复调用只退一次(refund_env):
    """Arq retry_max=2 → 同一任务最多跑 3 次，退款只能发生 1 次。

    不幂等 = 用户白赚 2 次配额。这是这个函数存在的全部理由。
    """
    from tasks.distill_task import _refund_quota_once

    for _ in range(3):  # 首跑 + 2 次重试
        await _refund_quota_once(task_id="dst_1", user_id=42)

    assert refund_env.refunded == [42], f"应只退 1 次，实际 {refund_env.refunded}"


@pytest.mark.asyncio
async def test_不同_task_id_各自退一次(refund_env):
    from tasks.distill_task import _refund_quota_once

    await _refund_quota_once(task_id="dst_1", user_id=42)
    await _refund_quota_once(task_id="dst_2", user_id=42)

    assert refund_env.refunded == [42, 42], "不同任务应各退各的"


# ---------------------------------------------------------------------------
# 3. 降级：Redis 挂了不能连带退款失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_不可用时仍退款(refund_env, monkeypatch):
    """Redis 不可用 → 降级为无锁，仍然要退。

    这里是个权衡：拿不到锁就可能重复退。但「不退款」意味着用户白被扣配额，
    比偶尔多退一次更糟。代码注释里也是这个取舍。
    """
    from tasks import distill_task
    from tasks.distill_task import _refund_quota_once

    async def boom():
        raise ConnectionError("Redis 宕机")

    monkeypatch.setattr(distill_task, "_make_refund_lock_client", boom)

    await _refund_quota_once(task_id="dst_1", user_id=42)

    assert refund_env.refunded == [42], "Redis 挂了也要退款，不能让用户白扣"


# ---------------------------------------------------------------------------
# 4. 退款自身失败不能破主流程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_退款失败不向上抛(refund_env, monkeypatch):
    """这个函数在 except 块里被调用，抛出去会盖掉真正的失败原因。"""
    from stashbox.backend.common import quota_service
    from tasks.distill_task import _refund_quota_once

    async def boom(session, user_id):
        raise RuntimeError("数据库炸了")

    monkeypatch.setattr(quota_service, "refund", boom)

    await _refund_quota_once(task_id="dst_1", user_id=42)  # 不应抛


# ---------------------------------------------------------------------------
# 5. 退款失败要还锁（2026-10 补）
#
# 锁的语义是「这笔已经退过了」，但它落锁的时刻**早于**退款成功的时刻 ——
# 中间任何一次失败都会留下一把「声称退过款、其实没退」的锁，之后 24h 内
# 所有 Arq 重试都被它挡在门外。用户于是为一次失败的蒸馏照付钱，且零告警。
# 这不是理论：退款失败有真实路径（乐观锁重试耗尽的 QuotaConflictError、
# 瞬时 DB 故障），而 Arq 的 retry 恰恰是这条链路上必然会发生的。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_退款失败会删掉幂等锁(refund_env, monkeypatch):
    from stashbox.backend.common import quota_service
    from tasks.distill_task import _refund_quota_once

    async def boom(session, user_id):
        raise RuntimeError("数据库炸了")

    monkeypatch.setattr(quota_service, "refund", boom)

    await _refund_quota_once(task_id="dst_1", user_id=42)

    # 判据是「DEL 确实打到那把锁上」：锁最终不在 store 里，正是因为它被删了
    assert "refund:dst_1" in refund_env.redis.deleted, "退款失败了却没还锁"
    assert "refund:dst_1" not in refund_env.redis.store, "锁还留着，重试将被永久挡掉"


@pytest.mark.asyncio
async def test_退款失败后_arq_重试能补上退款(refund_env, monkeypatch):
    """上面那条的业务后果：用户的钱要能在重试时拿回来。

    第一次退款失败（DB 抖一下）→ Arq 重试 → 第二次退款成功。判据是 `refunded`
    里出现了 user_id，也就是**这笔退款最终真的发生了**。
    """
    from stashbox.backend.common import quota_service
    from tasks.distill_task import _refund_quota_once

    calls = {"n": 0}

    async def flaky_refund(session, user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次：数据库抖了")
        refund_env.refunded.append(user_id)

    monkeypatch.setattr(quota_service, "refund", flaky_refund)

    await _refund_quota_once(task_id="dst_1", user_id=42)  # 首跑：退款失败
    await _refund_quota_once(task_id="dst_1", user_id=42)  # Arq retry：必须还能退

    assert refund_env.refunded == [42], "重试没补上退款 —— 用户为失败的蒸馏付了钱"


@pytest.mark.asyncio
async def test_退款成功后锁仍在_重复退仍被挡(refund_env):
    """还锁不能矫枉过正：退款成功时锁必须留着，幂等才是本分。

    和上一条成对看 —— 失败时删锁、成功时留锁，两条都绿才算修对。
    只实现前半条会变成「每次重试都退一次」，用户白赚 2~3 次配额。
    """
    from tasks.distill_task import _refund_quota_once

    for _ in range(3):  # 首跑 + 2 次重试，全部成功
        await _refund_quota_once(task_id="dst_1", user_id=42)

    assert refund_env.refunded == [42], f"退太多次了：{refund_env.refunded}"
    assert refund_env.redis.deleted == [], "退款成功时不该删锁"


def test_pipeline_退款失败也会还锁():
    """源码级断言：pipeline 那条退款路径同样接了释放锁。

    两个调用点各写一遍「失败就还锁」，漏掉一个就留下半条链 —— 而 pipeline
    是非 agent 路径（`DistillPipeline.run`），生产上两条都在跑。
    """
    src = (Path(AI_SERVICE_DIR) / "distill" / "pipeline.py").read_text()

    assert "release_refund_lock" in src, "pipeline 的退款失败没有还锁"
    # 还锁必须包在 refund 的失败分支里，而不是无条件执行
    refund_pos = src.index("await quota_service.refund(session, ctx.user_id)")
    release_pos = src.index("release_refund_lock(ctx.task_id")
    assert refund_pos < release_pos, "release 应该在 refund 之后（失败分支里）"
    # 还锁必须复用落锁时那个 client，否则单测的 FakeRedis 落的锁会被真 Redis 的
    # DEL 落空（测试看着绿，线上其实各连各的）
    assert "release_refund_lock(ctx.task_id, client=client)" in src, "还锁没复用落锁的 client"


# ---------------------------------------------------------------------------
# 6. 接线：失败路径确实调它
# ---------------------------------------------------------------------------


def test_蒸馏失败路径调用了退款():
    """源码级断言：`_refund_quota_once` 挂在失败分支上。

    防止以后有人重构 except 块时把这行删掉 —— 那会导致用户蒸馏失败白扣配额，
    而且不会有任何报错。
    """
    src = (Path(AI_SERVICE_DIR) / "tasks" / "distill_task.py").read_text()

    assert "_refund_quota_once(task_id=task_id, user_id=user_id)" in src
    # 必须在 `raise`（让 Arq 走 retry）之前调用
    refund_pos = src.index("_refund_quota_once(task_id=task_id")
    raise_pos = src.index("raise  # 让 Arq 走 retry 逻辑")
    assert refund_pos < raise_pos, "必须在 raise 之前退款，否则这条路径根本走不到"
