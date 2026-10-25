"""登录限流回归（2026-10-21）

wechat-login 是**未鉴权**端点，而当前实现是「code 派生 open_id，查不到就建号」。
两者相加就是一个无界的建号 + 刷免费配额的向量：

  - 任意 code 都能建一行 ``users``
  - 每建一个号就带一份免费配额（蒸馏要真花 LLM + TTS 的钱）
  - 内测把网关暴露到公网后，一次简单的循环就能打满

所以这个端点必须有 IP 维度的限流。本文件锁住：
  A. 超过阈值 → 429 + 业务信封（42901），**不是**裸 500
  B. 限额按 IP 隔离：换个 IP 不受影响（否则一人超限把所有人挡了）
  C. 限流只挡"尝试次数"，不挡正常登录 —— 阈值之内必须能正常建号登录
  D. 计数落在 Redis 里（多进程部署下多个 user-service 实例共享同一份额度）

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。

另含一条**软删账号不可登录**的回归（2026-10-22）：注销过的账号用旧 code
再登录，既不该拿到 token，也**不该凭空长出一行 users**。
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

LOGIN_URL = "/api/v1/auth/wechat-login"


def _load_module(name: str, rel: str):
    """服务目录名带连字符（user-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def user_main():
    return _load_module("_cp_login_rl_user_main", "user-service/main.py")


@pytest.fixture(autouse=True)
def small_limit(user_main, monkeypatch):
    """把阈值压到 3 次，测试才有意义（默认 20 次/分钟跑起来太慢）。"""
    monkeypatch.setattr(user_main, "_LOGIN_RATE_LIMIT", 3)
    monkeypatch.setattr(user_main, "_LOGIN_RATE_WINDOW", 60)


@pytest.fixture
async def client(user_main):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_main.app), base_url="http://user-service"
    ) as c:
        yield c


def _ip(exclude: str | None = None) -> str:
    """每个用例一个独立 IP —— 限流按 IP 分桶，天然隔离用例之间互不影响。

    `exclude` 保证「要比较的两个 IP」一定不是同一个：IP 池只有 250 个，
    两次独立随机有 **1/250（实测 0.39%）** 的概率抽到同一个 ——
    那样「换个 IP 不该受影响」那条用例就是**自己把自己限流了**
    （hot 用满了额度，cold 恰好是同一个 IP ⇒ 429 ⇒ 断言红）。
    表现是「单跑绿、全量跑红」的典型低频偶发失败（2026-10-25 全量套件撞到一次）。
    """
    for _ in range(1000):
        ip = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
        if ip != exclude:
            return ip
    raise RuntimeError("IP 池里取不到不冲突的地址（250 个池子不该发生）")


@pytest.fixture(autouse=True)
async def _reset_login_rate_limit():
    """清掉 Redis 里的登录限流计数，让本模块的用例与「之前跑过几遍」无关。

    为什么必须清（2026-10-22 实测）：
      计数落在 Redis（`ratelimit:login:{ip}`），**跨测试运行会残留**（TTL 60s）。
      `_ip()` 是随机 IP（198.51.100.0/24 里随机取），池子只有 250 个 ——
      连续跑几遍全量测试就可能撞上上一遍用过的 IP，带着旧计数进来，
      于是「换个 IP 不该受影响」那条用例直接 429（单跑绿、全量跑红）。

    → 限流状态是跨进程共享的外部状态，**每个用例开始前清一次**才是确定的。
    """
    from stashbox.backend.common.redis_client import get_redis_pool

    import redis.asyncio as aioredis

    r = aioredis.Redis(connection_pool=get_redis_pool())
    try:
        keys = await r.keys("ratelimit:login:*")
        if keys:
            await r.delete(*keys)
    finally:
        await r.aclose()
    yield


# ---------------------------------------------------------------------------
# A. 超阈值 → 429 + 业务信封
# ---------------------------------------------------------------------------
async def test_login_rate_limited_after_threshold(client):
    ip = _ip()
    headers = {"X-Forwarded-For": ip}

    for i in range(3):
        r = await client.post(
            LOGIN_URL, json={"code": f"rl_{uuid.uuid4().hex[:8]}"}, headers=headers
        )
        assert r.status_code == 200, f"第 {i + 1} 次不该被限流：{r.text}"

    r = await client.post(LOGIN_URL, json={"code": f"rl_{uuid.uuid4().hex[:8]}"}, headers=headers)

    assert r.status_code == 429, r.text
    body = r.json()
    # 信封而不是裸 500/裸 text —— 安卓按业务码分流，解析不出来就没有专门文案
    assert body["code"] == 42901, body
    assert body["data"] is None
    assert body["message"]


# ---------------------------------------------------------------------------
# B. 按 IP 隔离
# ---------------------------------------------------------------------------
def test_ip_pool_never_returns_the_excluded_address():
    """`_ip(exclude=...)` 必须真的换个地址（这条是 B 的前置条件）。

    为什么单独锁：IP 池只有 250 个，两次独立随机有 1/250 的概率撞上 ——
    撞上时「换个 IP 不该受影响」那条用例会**自己把自己限流**，
    表现为「单跑绿、全量跑红」的偶发失败（2026-10-25 全量套件撞到一次）。
    """
    for _ in range(500):
        base = _ip()
        assert _ip(exclude=base) != base


async def test_rate_limit_is_per_ip(client):
    """一个 IP 超限，不影响另一个 IP（否则一人刷爆，全内测用户都登不进去）。"""
    hot = _ip()
    cold = _ip(exclude=hot)

    for _ in range(4):
        await client.post(
            LOGIN_URL, json={"code": f"rl_{uuid.uuid4().hex[:8]}"}, headers={"X-Forwarded-For": hot}
        )

    r = await client.post(
        LOGIN_URL, json={"code": f"rl_{uuid.uuid4().hex[:8]}"}, headers={"X-Forwarded-For": cold}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


# ---------------------------------------------------------------------------
# C. 限流不误伤正常登录
# ---------------------------------------------------------------------------
async def test_login_under_limit_creates_user(client):
    """阈值之内必须能正常建号拿 token —— 限流不能把内测用户挡在门外。"""
    code = "rl_" + uuid.uuid4().hex[:12]
    r = await client.post(LOGIN_URL, json={"code": code}, headers={"X-Forwarded-For": _ip()})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user_id"]

    # 同一个 code 再登一次 → 同一个用户（open_id 由 code 派生）
    r2 = await client.post(LOGIN_URL, json={"code": code}, headers={"X-Forwarded-For": _ip()})
    assert r2.status_code == 200, r2.text
    assert r2.json()["user_id"] == body["user_id"]


# ---------------------------------------------------------------------------
# D. 计数落在 Redis（多实例共享份额度）
# ---------------------------------------------------------------------------
async def test_rate_limit_counter_lives_in_redis(user_main, client):
    """计数必须在 Redis 而不是进程内存里 —— user-service 多副本时进程内存计数
    等于每个副本各限各的，实际限额被放大 N 倍。"""
    from stashbox.backend.common import redis_client

    ip = _ip()
    for _ in range(4):
        await client.post(
            LOGIN_URL, json={"code": f"rl_{uuid.uuid4().hex[:8]}"}, headers={"X-Forwarded-For": ip}
        )

    r = redis_client.get_redis_pool()
    import redis.asyncio as aioredis

    redis_client_obj = aioredis.Redis(connection_pool=r)
    try:
        raw = await redis_client_obj.get(f"ratelimit:login:{ip}")
    finally:
        await redis_client_obj.aclose()

    assert raw is not None, "Redis 里没有计数 key —— 限流状态存在进程内存里，多副本会被放大"
    assert int(raw) >= 4


# ---------------------------------------------------------------------------
# E. 软删（注销过）的账号不可登录，且不会凭空建新号
# ---------------------------------------------------------------------------
async def test_soft_deleted_user_cannot_login_and_no_new_row(client):
    """注销后的账号用旧 code 登录 → 明确拒绝，**且 users 表不多出一行**。

    为什么这条重要（2026-10-22）：

    1. 注销端点承诺「注销后立即停止提供服务」，但登录只按 open_id 查、
       没过滤 deleted_at → 软删的账号照样能登录拿新 token，注销形同虚设。
    2. 注销会把 open_id 匿名化成 ``del_<uid>_<random>``，于是旧 code 再登录
       就**查不到**了 → 走「建新号」分支 → users 表 +1 行。内测期清一批
       测试账号，清理完一登录又长回来 —— 后台用户列表被这个来回撑大。
    """
    import asyncpg

    from stashbox.backend.common.config import settings

    code = "del_" + uuid.uuid4().hex[:12]
    r = await client.post(LOGIN_URL, json={"code": code}, headers={"X-Forwarded-For": _ip()})
    assert r.status_code == 200, r.text
    user_id = r.json()["user_id"]

    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database=settings.postgres_db,
    )
    try:
        await conn.execute("update users set deleted_at=now() where id=$1", int(user_id))
        before = await conn.fetchval("select count(*) from users")

        # 同一个 code 再登录 → 必须被拒
        r2 = await client.post(LOGIN_URL, json={"code": code}, headers={"X-Forwarded-For": _ip()})
        assert r2.status_code == 401, r2.text
        body = r2.json()
        assert body["code"] == 40102, body
        assert body["message"]

        after = await conn.fetchval("select count(*) from users")
        assert after == before, (
            f"软删账号再登录后 users 表多了一行（{before} → {after}）—— "
            "清理完一登录又长回来，用户列表就是这么被撑大的"
        )
    finally:
        await conn.close()
