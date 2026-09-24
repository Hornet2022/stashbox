"""动作 3（2026-09-24）：LLM 429 退避重试。

回归背景
--------
原实现是「429 直接抛 RateLimitError、不重试」—— 对**瞬时并发限流**极不友好：
2026-09-24 实测同一时刻单发 `doubao-seed-2.0-pro` 3/3 全 200，但并发就 429，
一次 429 直接让整条蒸馏失败（后续只能靠 Arq 重试整个任务，间隔长且重复消耗）。

本文件锁定两点：
1. `compute_backoff_seconds` 的退避性质（单调 / 429 更久 / 尊重 Retry-After / 不超 cap）
2. 429 会被**真的重试**，重试耗尽才抛 RateLimitError
"""

import asyncio

import httpx
import pytest

from llm import OpenAIClient
from llm.backoff import BACKOFF_CAP_SEC, compute_backoff_seconds
from llm.exceptions import RateLimitError
from llm.types import ChatMessage, ChatRequest


# ───────────────────────── 纯函数：退避计算 ─────────────────────────


def test_backoff_grows_with_attempt():
    """一般错误：退避随 attempt 递增（留出抖动余量，比较区间而非精确值）。"""
    vals = [compute_backoff_seconds(i) for i in range(4)]
    assert vals[0] < vals[3], f"应递增: {vals}"


def test_backoff_429_waits_longer_than_generic():
    """限流要比一般错误等更久（同样的 attempt 下）。"""
    for attempt in range(3):
        assert compute_backoff_seconds(attempt, is_rate_limited=True) > compute_backoff_seconds(
            attempt
        )


def test_backoff_respects_retry_after():
    """服务端给了 Retry-After → 至少等那么久（受 cap 约束）。"""
    delay = compute_backoff_seconds(0, is_rate_limited=True, retry_after="30")
    assert delay >= 30 * (1 - 0.25)  # 允许抖动下浮


def test_backoff_ignores_non_numeric_retry_after():
    """Retry-After 可能是 HTTP-date（非数字）→ 不能炸，回退指数值。"""
    delay = compute_backoff_seconds(
        0, is_rate_limited=True, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"
    )
    assert delay > 0


def test_backoff_never_exceeds_cap():
    """cap 必须约束**最终值**（含抖动）—— 否则退避会撑爆 Arq 的 job_timeout。"""
    for attempt in range(12):
        assert 0 <= compute_backoff_seconds(attempt, is_rate_limited=True) <= BACKOFF_CAP_SEC + 1e-9
        assert 0 <= compute_backoff_seconds(attempt) <= BACKOFF_CAP_SEC + 1e-9


# ───────────────────────── 行为：429 会被重试 ─────────────────────────


def _client_with(handler, max_retries: int = 3) -> OpenAIClient:
    client = OpenAIClient(
        base_url="https://fake.test/v1",
        model="fake-model",
        api_key="fake-key",
        max_retries=max_retries,
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.fixture
def _no_real_sleep(monkeypatch):
    """把 asyncio.sleep 换成 no-op —— 测试别真等退避。"""

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)


async def test_retries_on_429_then_succeeds(_no_real_sleep):
    """前两次 429、第三次成功 → 应成功返回（修复前会直接抛 RateLimitError）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = _client_with(handler, max_retries=3)
    resp = await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert calls["n"] == 3, f"应当重试到成功，实际请求 {calls['n']} 次"
    assert resp.content == "ok"


async def test_raises_rate_limit_after_exhausting_retries(_no_real_sleep):
    """全程 429 → 重试耗尽后仍抛 RateLimitError（保留上游可识别语义）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate"})

    client = _client_with(handler, max_retries=2)
    with pytest.raises(RateLimitError):
        await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert calls["n"] == 2, f"应当把 max_retries 用完，实际 {calls['n']} 次"
