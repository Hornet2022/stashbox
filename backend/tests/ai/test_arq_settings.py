"""Arq 配置加载测试（任务包 §4.3）。

只测 load_arq_config 的默认值 + 环境变量覆盖，不连 Redis。
"""
import pytest
from dataclasses import FrozenInstanceError

from arq_settings import ArqConfig, load_arq_config


@pytest.fixture(autouse=True)
def _clear_arq_env(monkeypatch):
    """每个 case 都从「没设任何 ARQ_* / REDIS_URL」开始，防止测试间互相污染。"""
    for key in ("REDIS_URL", "ARQ_MAX_JOBS", "ARQ_JOB_TIMEOUT", "ARQ_RESULT_TTL", "ARQ_RETRY_MAX"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
def test_defaults():
    cfg = load_arq_config()

    assert cfg == ArqConfig(
        redis_url="redis://localhost:6379/0",
        queue_name="stashbox:distill",
        max_jobs=4,
        job_timeout_sec=1800,
        result_ttl_sec=86400,
        retry_max=2,
    )


def test_default_queue_name_matches_worker_and_dispatcher():
    """队列名是 worker 和 dispatcher 的唯一约定，写死成常量避免两边漂移。"""
    import worker

    assert load_arq_config().queue_name == worker.WorkerSettings.queue_name


def test_config_is_frozen():
    cfg = load_arq_config()

    with pytest.raises(FrozenInstanceError):
        cfg.redis_url = "redis://other:6379/0"


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------
def test_redis_url_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis.internal:6380/3")

    cfg = load_arq_config()

    assert cfg.redis_url == "redis://redis.internal:6380/3"


@pytest.mark.parametrize(
    "env_key,field,value,expected",
    [
        ("ARQ_MAX_JOBS", "max_jobs", "16", 16),
        ("ARQ_JOB_TIMEOUT", "job_timeout_sec", "60", 60),
        ("ARQ_RESULT_TTL", "result_ttl_sec", "3600", 3600),
        ("ARQ_RETRY_MAX", "retry_max", "0", 0),
    ],
)
def test_numeric_env_overrides(monkeypatch, env_key, field, value, expected):
    monkeypatch.setenv(env_key, value)

    cfg = load_arq_config()

    assert getattr(cfg, field) == expected
    assert isinstance(getattr(cfg, field), int)  # 不能是字符串


def test_all_env_overrides_together(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("ARQ_MAX_JOBS", "8")
    monkeypatch.setenv("ARQ_JOB_TIMEOUT", "900")
    monkeypatch.setenv("ARQ_RESULT_TTL", "7200")
    monkeypatch.setenv("ARQ_RETRY_MAX", "5")

    assert load_arq_config() == ArqConfig(
        redis_url="redis://cache:6379/1",
        queue_name="stashbox:distill",
        max_jobs=8,
        job_timeout_sec=900,
        result_ttl_sec=7200,
        retry_max=5,
    )


def test_reload_picks_up_new_env(monkeypatch):
    """配置是每次 load 都重读 env（不是 import 时定死）。"""
    monkeypatch.setenv("ARQ_MAX_JOBS", "2")
    assert load_arq_config().max_jobs == 2

    monkeypatch.setenv("ARQ_MAX_JOBS", "9")
    assert load_arq_config().max_jobs == 9
