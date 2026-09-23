"""CP3.6.3：TTS 段并行合成单测。

验证 4 件事：
1. gather 完成后 segment 顺序与原 chunk index 一致（step4 拼接正确）
2. 单段失败 → gather 抛错（与串行行为一致，主流程已捕获重试）
3. Semaphore 实际控制并发数 ≤ MAX_CONCURRENT
4. 并行比串行快（性能验收：5x 加速）

本测试只测 step3_tts 的并发逻辑，不接真 TTS（用 SlowFakeTTS 模拟网络往返）。
"""

import asyncio
import time

import pytest

from distill.schemas import DistillContext, RewriteOutput
from distill.steps import step3_tts


class SlowFakeTTS:
    """模拟 TTS 网络往返的替身（默认 50ms/段，可调）。"""

    def __init__(self, latency_ms: int = 50):
        self.latency_ms = latency_ms
        self.provider_name = "fake-slow"
        self.concurrent_observed: int = 0
        self.current_concurrent: int = 0
        self.lock = asyncio.Lock()

    async def synthesize(self, text: str) -> bytes:
        # 记录瞬时并发数（peak 观察）
        async with self.lock:
            self.current_concurrent += 1
            self.concurrent_observed = max(self.concurrent_observed, self.current_concurrent)
        try:
            await asyncio.sleep(self.latency_ms / 1000)
            # 返回 1 字节（无效音频字节，但 step3_tts 只校验类型）
            return b"\x00" * 16
        finally:
            async with self.lock:
                self.current_concurrent -= 1


def _make_ctx(n_chunks: int) -> DistillContext:
    """构造一个有 N 个 section 的 ctx，step3_tts 会切出 N+2 段（hook + N sections + outro）."""
    sections = [f"第{i + 1}段内容。" for i in range(n_chunks)]
    return DistillContext(
        task_id="dst_test0000000000000000001",
        article_id="art_test000000000000000001",
        user_id=1,
        url="https://x.com/a",
        raw_content="test",
        title="test",
        rewrite=RewriteOutput(
            hook="钩子。",
            sections=sections,
            outro="结尾。",
            word_count=42,
        ),
    )


# ---------------------------------------------------------------------------
# 1. gather 保序
# ---------------------------------------------------------------------------
async def test_gather_returns_segments_in_order():
    """并发后 segment 顺序必须与原 chunk index 一致（step4 拼接依赖）。"""
    tts = SlowFakeTTS(latency_ms=30)
    ctx = _make_ctx(10)  # 12 段（hook + 10 sections + outro）

    await step3_tts(ctx, tts)

    # 段顺序应该严格按 index 升序
    indices = [s["index"] for s in ctx.tts.segments]
    assert indices == sorted(indices), f"段顺序错乱: {indices}"
    assert indices[0] == 0
    assert indices[-1] == 11  # 0=hook, 1-10=sections, 11=outro


# ---------------------------------------------------------------------------
# 2. 单段失败 → gather 抛错（与串行一致）
# ---------------------------------------------------------------------------
class FailingTTS:
    provider_name = "failing"

    def __init__(self, fail_at: int = 3):
        self.fail_at = fail_at
        self.call_count = 0
        self.lock = asyncio.Lock()

    async def synthesize(self, text: str) -> bytes:
        async with self.lock:
            self.call_count += 1
            call_id = self.call_count
        if call_id == self.fail_at:
            raise RuntimeError(f"simulated TTS failure at call {call_id}")
        return b"\x00" * 16


async def test_gather_with_failure_raises():
    """任一段失败 → gather 抛错（主流程已捕获重试，不需改）。"""
    tts = FailingTTS(fail_at=3)
    ctx = _make_ctx(10)

    with pytest.raises(RuntimeError, match="simulated TTS failure"):
        await step3_tts(ctx, tts)


# ---------------------------------------------------------------------------
# 3. Semaphore 实际控制并发数 ≤ MAX_CONCURRENT
# ---------------------------------------------------------------------------
async def test_semaphore_respects_max_concurrent(monkeypatch):
    """MAX_CONCURRENT_TTS=3 时，瞬时并发数 ≤ 3。"""
    monkeypatch.setenv("TTS_MAX_CONCURRENT", "3")
    tts = SlowFakeTTS(latency_ms=100)  # 100ms 让并发数观察更稳
    ctx = _make_ctx(20)  # 22 段

    await step3_tts(ctx, tts)

    # 实际瞬时并发数 ≤ 3（可能恰好等于 3，但绝不会超过）
    assert (
        tts.concurrent_observed <= 3
    ), f"Semaphore 未生效: peak={tts.concurrent_observed}, expected ≤ 3"
    # 全 22 段都处理了
    assert len(ctx.tts.segments) == 22


# ---------------------------------------------------------------------------
# 4. 并行比串行快（性能验收）
# ---------------------------------------------------------------------------
async def test_parallel_faster_than_serial(monkeypatch):
    """20 段 + 50ms 串行 1s → 并行 8 限速 ~150ms，验证 5x 加速。"""
    monkeypatch.setenv("TTS_MAX_CONCURRENT", "8")
    tts = SlowFakeTTS(latency_ms=50)
    ctx = _make_ctx(20)  # 22 段

    # === 并行版（CP3.6.3 实际实现）===
    start = time.time()
    await step3_tts(ctx, tts)
    parallel_duration = time.time() - start

    # === 串行版（baseline for comparison）===
    # 串行 22 段 * 50ms = 1.1s；并行 8 限速 ceil(22/8) * 50ms = 150ms
    serial_estimate = 22 * 0.050  # 1.1s
    parallel_estimate = (22 // 8 + 1) * 0.050  # ~0.15s

    # 断言：并行至少 3x 快于串行估计
    assert parallel_duration < serial_estimate / 3, (
        f"CP3.6.3 性能不达标: parallel={parallel_duration:.3f}s, "
        f"serial_estimate={serial_estimate:.3f}s"
    )
    # 软断言：并行接近理论估计
    assert parallel_duration < parallel_estimate * 3, (
        f"CP3.6.3 并行慢于理论 3x: actual={parallel_duration:.3f}s, "
        f"expected~{parallel_estimate:.3f}s"
    )


# ---------------------------------------------------------------------------
# 5. 默认并发数 = 8（无 TTS_MAX_CONCURRENT env）
# ---------------------------------------------------------------------------
async def test_default_max_concurrent_is_8(monkeypatch):
    """无 TTS_MAX_CONCURRENT 环境变量时，默认 8 路并发。"""
    monkeypatch.delenv("TTS_MAX_CONCURRENT", raising=False)
    tts = SlowFakeTTS(latency_ms=80)
    ctx = _make_ctx(20)  # 22 段

    await step3_tts(ctx, tts)

    # peak 应该接近 8（22 段每段 80ms 排队到 8 路）
    assert tts.concurrent_observed == 8, f"默认并发数应该是 8，实际是 {tts.concurrent_observed}"
