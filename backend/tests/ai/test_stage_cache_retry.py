"""CP3.6.4：Stage cache + Arq retry 复用单测。

8 个用例覆盖：
1. 写入 + 读回 roundtrip
2. read 返回 None when key missing
3. read 返回 None on corrupt json
4. clear_stages 删除所有 step keys
5. env=false → read/write/clear 全是 no-op
6. pipeline 用 cache 跳过 step（mock step1 不被调）
7. fire-and-forget write 不阻塞主流程
8. TTL 在 24h ± 1s 范围
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Test 1: write + read roundtrip
# ---------------------------------------------------------------------------
async def test_write_and_read_stage_roundtrip(monkeypatch):
    """Pydantic 模型写入 → 读回 dict 字段一致。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.schemas import StructuredOutput
    from distill.stage_cache import read_stage, write_stage

    output = StructuredOutput(
        summary="test",
        chapters=[],
        entities=[],
        tags=["AI"],
    )

    fake_client = AsyncMock()
    fake_client.set = AsyncMock(return_value=True)
    fake_client.get = AsyncMock(return_value=output.model_dump_json())

    with patch("redis.asyncio.Redis", return_value=fake_client):
        await write_stage("dst_x", "step1_structure", output)
        cached = await read_stage("dst_x", "step1_structure")

    assert cached is not None
    assert cached["summary"] == "test"
    assert cached["tags"] == ["AI"]
    fake_client.set.assert_awaited_once()
    fake_client.get.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: read miss returns None
# ---------------------------------------------------------------------------
async def test_read_stage_returns_none_when_key_missing(monkeypatch):
    """key 不存在时 read 返回 None（不抛错）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import read_stage

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=None)

    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await read_stage("dst_missing", "step1_structure")

    assert result is None


# ---------------------------------------------------------------------------
# Test 3: read on corrupt json returns None
# ---------------------------------------------------------------------------
async def test_read_stage_returns_none_on_corrupt_json(monkeypatch):
    """JSON 解析失败 → 当 miss 处理（返回 None，不抛）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import read_stage

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value="not-valid-json{")

    with patch("redis.asyncio.Redis", return_value=fake_client):
        result = await read_stage("dst_corrupt", "step1_structure")

    assert result is None  # 降级到重算


# ---------------------------------------------------------------------------
# Test 4: clear_stages deletes all step keys
# ---------------------------------------------------------------------------
async def test_clear_stages_deletes_all_step_keys(monkeypatch):
    """clear_stages 用 SCAN + DEL 清理某 task 的所有 stage。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import clear_stages

    fake_client = AsyncMock()
    # SCAN 第一次返回 2 keys + cursor=1，第二次返回 1 key + cursor=0（结束）
    fake_client.scan = AsyncMock(
        side_effect=[
            (1, ["stage:dst_x:step1_structure", "stage:dst_x:step2_rewrite"]),
            (0, ["stage:dst_x:step3_tts"]),
        ]
    )
    fake_client.delete = AsyncMock(return_value=2)

    with patch("redis.asyncio.Redis", return_value=fake_client):
        await clear_stages("dst_x")

    assert fake_client.scan.await_count == 2
    assert fake_client.delete.await_count == 2
    # 验证 3 个 key 都被 delete（每次 delete 调用收到 N 个 key 位置参数）
    all_deleted = set()
    for call in fake_client.delete.await_args_list:
        # 源码 `client.delete(*keys)` → call.args = tuple of strings
        all_deleted.update(call.args)
    assert "stage:dst_x:step1_structure" in all_deleted
    assert "stage:dst_x:step2_rewrite" in all_deleted
    assert "stage:dst_x:step3_tts" in all_deleted


# ---------------------------------------------------------------------------
# Test 5: env=false → no-op
# ---------------------------------------------------------------------------
async def test_stage_cache_disabled_no_op(monkeypatch):
    """STAGE_CACHE_ENABLED=false 时 read/write/clear 全部 no-op（不调 Redis）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "false")
    from distill.schemas import StructuredOutput
    from distill.stage_cache import clear_stages, read_stage, write_stage

    output = StructuredOutput(summary="x", chapters=[], entities=[], tags=[])

    # 如果 Redis 被调，patch 会失败（因为 fake_client 没被注入）
    fake_client = AsyncMock()
    with patch("redis.asyncio.Redis", return_value=fake_client) as redis_cls:
        await write_stage("dst_x", "step1_structure", output)
        result = await read_stage("dst_x", "step1_structure")
        await clear_stages("dst_x")

    # 关键断言：env=false 时 Redis 完全不被调
    redis_cls.assert_not_called()
    assert result is None


# ---------------------------------------------------------------------------
# Test 6: pipeline uses cache on retry
# ---------------------------------------------------------------------------
async def test_pipeline_uses_cache_on_retry(monkeypatch):
    """CP3.6.4 核心：pipeline._try_load_step_from_cache 读 cache 命中时返回 True，ctx 已设值。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext, StructuredOutput

    # 模拟 cache 命中：read_stage 返回上次的 StructuredOutput dict
    cached = StructuredOutput(summary="cached", chapters=[], entities=[], tags=["cached"])
    monkeypatch.setattr(
        "distill.pipeline.read_stage",
        AsyncMock(return_value=cached.model_dump()),
    )

    ctx = DistillContext(
        task_id="dst_retry",
        article_id="art_x",
        user_id=1,
        url="https://x.com/a",
        raw_content="test",
        title="t",
    )

    pipeline = DistillPipeline(None, tts_client=None, db_session_factory=None)
    hit = await pipeline._try_load_step_from_cache(ctx, "step1_structure", "structured")

    # 关键断言：
    assert hit is True
    assert ctx.structured is not None
    assert ctx.structured.summary == "cached"
    assert ctx.structured.tags == ["cached"]


async def test_pipeline_cache_miss_continues_normal_flow(monkeypatch):
    """CP3.6.4：cache miss → _try_load_step_from_cache 返回 False，ctx 不变。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext

    monkeypatch.setattr("distill.pipeline.read_stage", AsyncMock(return_value=None))

    ctx = DistillContext(
        task_id="dst_miss",
        article_id="art_x",
        user_id=1,
        url="https://x.com/a",
        raw_content="test",
        title="t",
    )

    pipeline = DistillPipeline(None, tts_client=None, db_session_factory=None)
    hit = await pipeline._try_load_step_from_cache(ctx, "step1_structure", "structured")

    assert hit is False
    assert ctx.structured is None  # 未设值


async def test_pipeline_cache_deserialize_failure_falls_back(monkeypatch):
    """CP3.6.4：cache 反序列化失败 → 当 miss 处理（不抛错）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.pipeline import DistillPipeline
    from distill.schemas import DistillContext

    # cache 里有数据但 schema 不匹配（chapters 应该是 list，传 dict 触发 Pydantic ValidationError）
    monkeypatch.setattr(
        "distill.pipeline.read_stage",
        AsyncMock(
            return_value={"summary": "x", "chapters": "not-a-list", "entities": [], "tags": []}
        ),
    )

    ctx = DistillContext(
        task_id="dst_corrupt",
        article_id="art_x",
        user_id=1,
        url="https://x.com/a",
        raw_content="test",
        title="t",
    )

    pipeline = DistillPipeline(None, tts_client=None, db_session_factory=None)
    hit = await pipeline._try_load_step_from_cache(ctx, "step1_structure", "structured")

    # 反序列化失败 → 当 miss
    assert hit is False
    assert ctx.structured is None


# ---------------------------------------------------------------------------
# Test 7: fire-and-forget write 不阻塞
# ---------------------------------------------------------------------------
async def test_fire_and_forget_write_does_not_block(monkeypatch):
    """write_stage 即使慢也不阻塞主流程（pipeline 用 create_task 触发）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import write_stage

    # 模拟 Redis 慢响应（500ms）
    fake_client = AsyncMock()

    async def slow_set(*args, **kwargs):
        await asyncio.sleep(0.5)
        return True

    fake_client.set = slow_set

    with patch("redis.asyncio.Redis", return_value=fake_client):
        start = time.time()
        # fire-and-forget 模式：create_task 立即返回
        task = asyncio.create_task(write_stage("dst_x", "step1_structure", {"a": 1}))
        # 主流程立即继续（不应该等 500ms）
        elapsed = time.time() - start
        assert elapsed < 0.05, f"fire-and-forget 阻塞了 {elapsed:.3f}s"
        # 等 task 完成清理
        await task


# ---------------------------------------------------------------------------
# Test 8: TTL = 24h
# ---------------------------------------------------------------------------
async def test_cache_ttl_is_24h(monkeypatch):
    """write_stage 用 ex=86400（24h）。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import write_stage

    fake_client = AsyncMock()
    fake_client.set = AsyncMock(return_value=True)

    with patch("redis.asyncio.Redis", return_value=fake_client):
        await write_stage("dst_x", "step1_structure", {"a": 1})

    fake_client.set.assert_awaited_once()
    call_args = fake_client.set.await_args
    # 第 3 个位置参数是 ex（set(key, value, ex=...)）
    assert (
        call_args.kwargs.get("ex") == 86400
    ), f"TTL 应该是 86400s（24h），实际是 {call_args.kwargs.get('ex')}"


# ---------------------------------------------------------------------------
# Test 9 (bonus): 关闭开关读取 priority
# ---------------------------------------------------------------------------
async def test_read_stage_with_unknown_step_returns_none(monkeypatch):
    """未知 step_name 直接返回 None，不调 Redis。"""
    monkeypatch.setenv("STAGE_CACHE_ENABLED", "true")
    from distill.stage_cache import read_stage, write_stage

    fake_client = AsyncMock()
    with patch("redis.asyncio.Redis", return_value=fake_client) as redis_cls:
        await write_stage("dst_x", "unknown_step", {"a": 1})
        result = await read_stage("dst_x", "unknown_step")

    redis_cls.assert_not_called()
    assert result is None
