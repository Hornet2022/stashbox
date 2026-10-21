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


def _ip() -> str:
    """每个用例一个独立 IP —— 限流按 IP 分桶，天然隔离用例之间互不影响。"""
    return f"198.51.100.{uuid.uuid4().int % 250 + 1}"


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
async def test_rate_limit_is_per_ip(client):
    """一个 IP 超限，不影响另一个 IP（否则一人刷爆，全内测用户都登不进去）。"""
    hot = _ip()
    cold = _ip()

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
