"""CostTracker 单测（任务包 §4.3）。

用假 redis client 注入（`CostTracker(client=...)`），不连本机 Redis。
"""
from datetime import date

import pytest

from observability import cost_tracker as ct_mod
from observability.cost_tracker import CostTracker, estimate_cost_usd

CLAUDE = "claude-4-sonnet-20250514"
QWEN = "qwen2.5-vl-72b-instruct"
MOCK = "mock-model"


class FakePipeline:
    def __init__(self, owner):
        self.owner = owner
        self.commands: list[tuple] = []

    def hincrbyfloat(self, key, field, amount):
        self.commands.append(("hincrbyfloat", key, field, amount))
        return self

    def hincrby(self, key, field, amount):
        self.commands.append(("hincrby", key, field, amount))
        return self

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))
        return self

    async def execute(self):
        return [None] * len(self.commands)


class FakeRedis:
    def __init__(self):
        self.pipelines: list[FakePipeline] = []
        self.closed = False

    def pipeline(self):
        pipe = FakePipeline(self)
        self.pipelines.append(pipe)
        return pipe

    async def aclose(self):
        self.closed = True


class BoomRedis(FakeRedis):
    def pipeline(self):
        pipe = FakePipeline(self)
        pipe.execute = self._boom  # type: ignore[method-assign]
        self.pipelines.append(pipe)
        return pipe

    async def _boom(self):
        raise ConnectionError("redis down")


def _tracker(client=None) -> CostTracker:
    return CostTracker(client=client or FakeRedis())


# ---------------------------------------------------------------------------
# 单价换算
# ---------------------------------------------------------------------------
def test_claude_1k_prompt_plus_1k_completion_costs_18_mill():
    assert estimate_cost_usd(CLAUDE, 1_000, 1_000) == pytest.approx(0.018)


def test_qwen_1k_prompt_plus_1k_completion_costs_8_mill():
    assert estimate_cost_usd(QWEN, 1_000, 1_000) == pytest.approx(0.008)


def test_mock_model_is_free():
    assert estimate_cost_usd(MOCK, 10_000, 10_000) == pytest.approx(0.0)


def test_unknown_model_is_free_instead_of_crashing():
    assert estimate_cost_usd("some-future-model", 1_000, 1_000) == pytest.approx(0.0)


def test_cost_scales_linearly_with_tokens():
    assert estimate_cost_usd(CLAUDE, 2_000, 500) == pytest.approx(0.003 * 2 + 0.015 * 0.5)


# ---------------------------------------------------------------------------
# Redis 写入
# ---------------------------------------------------------------------------
async def test_record_returns_cost_usd():
    cost = await _tracker().record_llm_usage(1, "art_1", CLAUDE, 1_000, 1_000)

    assert cost == pytest.approx(0.018)


async def test_record_writes_user_article_and_global_keys():
    redis_client = FakeRedis()

    await _tracker(redis_client).record_llm_usage(7, "art_abc", QWEN, 1_000, 1_000)

    commands = redis_client.pipelines[0].commands
    today = date.today().isoformat()

    assert ("hincrbyfloat", f"cost:user:7:{today}", QWEN, pytest.approx(0.008)) in commands
    assert ("hincrbyfloat", "cost:article:art_abc", QWEN, pytest.approx(0.008)) in commands
    assert ("hincrbyfloat", f"cost:global:{today}", QWEN, pytest.approx(0.008)) in commands
    assert ("hincrby", f"cost:global:{today}", "total_requests", 1) in commands


async def test_record_sets_ttl_per_key():
    redis_client = FakeRedis()

    await _tracker(redis_client).record_llm_usage(7, "art_abc", CLAUDE, 1, 1)

    expires = [cmd for cmd in redis_client.pipelines[0].commands if cmd[0] == "expire"]
    ttls = [cmd[2] for cmd in expires]
    keys = [cmd[1] for cmd in expires]
    assert ttls == [86400 * 30, 86400 * 7, 86400 * 90]  # user / article / global
    assert keys[0].startswith("cost:user:7:")
    assert keys[1] == "cost:article:art_abc"
    assert keys[2].startswith("cost:global:")


async def test_injected_client_is_not_closed_by_tracker():
    redis_client = FakeRedis()

    await _tracker(redis_client).record_llm_usage(7, "art_abc", CLAUDE, 1, 1)

    assert redis_client.closed is False  # 注入的 client 生命周期归调用方


async def test_redis_failure_is_swallowed():
    """归因写失败只 log，不能把蒸馏任务打挂（任务包 §8）。"""
    cost = await _tracker(BoomRedis()).record_llm_usage(7, "art_abc", CLAUDE, 1_000, 1_000)

    assert cost == pytest.approx(0.018)  # 金额照算，只是没落库


async def test_price_table_covers_priced_models():
    """单价表改动时要有人 review —— 三个 model 的单价钉住。"""
    assert ct_mod.COST_PER_1K_TOKENS[CLAUDE] == {"input": 0.003, "output": 0.015}
    assert ct_mod.COST_PER_1K_TOKENS[QWEN] == {"input": 0.002, "output": 0.006}
    assert ct_mod.COST_PER_1K_TOKENS[MOCK] == {"input": 0.0, "output": 0.0}
