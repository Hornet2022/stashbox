"""auto_retry 的 Redis 计数器必须走共享连接池（2026-10）。

## 缺陷

`get_user_daily_retry_count` / `bump_user_daily_retry_count` 原来自己
`aioredis.from_url(f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}")`
现建连接池。这不是「风格问题」，是**限流失效**：

  `settings.redis_url`（common/config.py）拼 URL 时带密码 ——
  `redis://:<password>@host:port/db`；手搓的那条不带。Redis 一启用密码，
  这两个函数每次都认证失败，而 except 把失败吞成 `return 0`。于是：

    读 → 恒为 0 → `0 >= 3` 永远不成立 → 限流永远放行
    写 → 恒为 0 → 计数器永远不动

  「单用户每天最多 3 次」这条防重蒸风暴的闸门，在配了密码的环境里等于没有。
  而它本来就是为了防「一次低评把队列打爆」才写的。

## 判据为什么盯「连接参数」而不是「函数返回值」

函数在 Redis 挂掉时按设计返回 0（降级），所以断言返回值区分不了
「读到了真 0」和「认证失败降级成 0」—— 恰好把最该抓的那种失败放过了。
真正的判据是**发出去的连接参数里带没带密码**，以及**用的是不是共享池**。
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from distill import auto_retry as ar  # noqa: E402


class FakePool:
    """只记「谁用了这个池」的替身。"""

    def __init__(self, url: str):
        self.url = url
        self.used_by: list[str] = []


class FakeRedis:
    """够用的 redis.asyncio.Redis 替身。"""

    def __init__(self, *, pool=None, from_url_calls=None):
        self.pool = pool
        self.from_url_calls = from_url_calls
        self.ops: list[tuple] = []

    async def get(self, key):
        self.ops.append(("get", key))
        return None

    async def incr(self, key):
        self.ops.append(("incr", key))
        return 1

    async def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    async def aclose(self):
        self.ops.append(("aclose",))


@pytest.fixture
def shared_pool(monkeypatch):
    """把 common.redis_client.get_redis_pool 换成可观测的假池。

    注意只 patch 这**一个** seam，不动 redis 模块本身 —— 早先在退款测试里
    patch 全局 `redis.asyncio.Redis` 造成过 cache_service 缓存被污染、
    一次全量跑出 57 failed 的事故。
    """
    import redis.asyncio as aioredis

    from stashbox.backend.common import redis_client

    pool = FakePool("redis://:s3cret@localhost:6379/0")
    built: list[FakeRedis] = []

    def _make(*, connection_pool=None):
        pool.used_by.append("Redis(connection_pool=...)")
        c = FakeRedis(pool=connection_pool)
        built.append(c)
        return c

    def _forbidden_from_url(*a, **k):
        raise AssertionError(
            "auto_retry 又在自建连接池（from_url）—— "
            "这样会漏掉 settings.redis_url 里的密码，且每次调用多一次握手"
        )

    monkeypatch.setattr(redis_client, "get_redis_pool", lambda: pool)
    monkeypatch.setattr(aioredis, "Redis", _make)
    monkeypatch.setattr(aioredis, "from_url", _forbidden_from_url)

    return type("Env", (), {"pool": pool, "clients": built})()


# ---------------------------------------------------------------------------
# 1. 走共享池，不再自建
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_读计数走共享池(shared_pool):
    await ar.get_user_daily_retry_count(7)

    assert shared_pool.pool.used_by == ["Redis(connection_pool=...)"]
    assert len(shared_pool.clients) == 1


@pytest.mark.asyncio
async def test_写计数走共享池(shared_pool):
    await ar.bump_user_daily_retry_count(7)

    assert shared_pool.pool.used_by == ["Redis(connection_pool=...)"]


@pytest.mark.asyncio
async def test_一次重试的两个计数器_共用同一个池(shared_pool):
    """hooks_impl 里是先 bump 再读，两者必须落在同一个池上。

    两个函数各建各的池不只是慢 —— 它们计的是**同一个 key**
    （`stashbox:auto_retry:count:{user_id}`），池一分开就没法保证
    读写看到同一份状态，限流也就不可信了。
    """
    await ar.bump_user_daily_retry_count(7)
    await ar.get_user_daily_retry_count(7)

    assert shared_pool.pool.used_by == [
        "Redis(connection_pool=...)",
        "Redis(connection_pool=...)",
    ]


def test_代码里不再出现_from_url():
    """源码级兜底：上面几条靠 monkeypatch `Redis` 才能抓住 from_url 路径。

    用 ast 而不是 `'from_url' not in src` 全文匹配 —— 后者会命中本模块自己
    解释这个缺陷的 docstring，把「说明文字」误判成「代码」。这里只认真正
    的属性访问与函数调用。

    将来有人图省事写回 from_url，这几条会一起红（from_url 被替换成会抛的版本），
    但只有这条能在**没有跑异步调用**的场景下也给出信号。
    """
    import ast

    path = BACKEND_DIR / "ai-service" / "distill" / "auto_retry.py"
    tree = ast.parse(path.read_text())

    offending = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr in ("from_url", "redis_password"))
        or (isinstance(node, ast.Name) and node.id == "from_url")
    ]

    assert not offending, (
        f"这些位置又在自建连接池/手搓密码了：{offending} —— "
        "从_url 不带 settings.redis_url 里的密码"
    )


# ---------------------------------------------------------------------------
# 2. 密码确实进了连接参数（这才是缺陷的核心）
# ---------------------------------------------------------------------------


def test_共享池的_url_带密码():
    """反向确认前提：settings.redis_url 本身带密码。

    修复只是让 auto_retry **复用**这个 URL。如果哪天 redis_url 不带密码了，
    上面那些测试照样绿 —— 缺陷会以另一种形式回来。这条把前提钉住。
    """
    from stashbox.backend.common.config import settings

    if not settings.redis_password:
        pytest.skip("本地未配 redis 密码，跳过（生产会启用）")

    assert (
        f":{settings.redis_password}@" in settings.redis_url
    ), "redis_url 没带密码 —— auto_retry 改走共享池也只能认证失败"


@pytest.mark.asyncio
async def test_密码缺失时_读计数会降级成_0(monkeypatch):
    """把「认证失败」这个真实故障注入进来，确认降级路径成立。

    顺带说明为什么不能靠返回值判断健康：这里返回 0，和「真的没有记录」
    完全一样。这就是原缺陷能活这么久的原因 —— 它看起来一直正常运行。
    """
    from stashbox.backend.common import redis_client

    class _AuthFailPool(FakePool):
        pass

    def _raise():
        raise ConnectionError("NOAUTH Authentication required")

    monkeypatch.setattr(redis_client, "get_redis_pool", _raise)

    assert await ar.get_user_daily_retry_count(7) == 0
    assert await ar.bump_user_daily_retry_count(7) == 0


# ---------------------------------------------------------------------------
# 3. 计数器语义没被改坏
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_首次计数设置_24h_过期(shared_pool):
    await ar.bump_user_daily_retry_count(7)

    (client,) = shared_pool.clients
    assert (
        "expire",
        "stashbox:auto_retry:count:7",
        24 * 3600,
    ) in client.ops, "计数器没有过期时间 → 用户的「每天 3 次」会变成「一辈子 3 次」"


@pytest.mark.asyncio
async def test_用的_key_没变(shared_pool):
    """换连接池不该顺手换 key —— 存量计数得继续被认出来。

    存量 key 是线上真实存在的数据。key 一改，所有用户的今日计数归零，
    当天所有人都能再刷 3 次。
    """
    await ar.bump_user_daily_retry_count(42)
    await ar.get_user_daily_retry_count(42)

    keys = [op[1] for c in shared_pool.clients for op in c.ops if op[1]]
    assert keys, "没有任何 key 被操作"
    assert set(keys) == {"stashbox:auto_retry:count:42"}, f"key 变了：{set(keys)}"
