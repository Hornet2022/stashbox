"""CP3.6.4：Stage 缓存（Redis），让 Arq retry 复用已成功的 step 结果。

设计要点：
- key 格式：stage:{task_id}:{step_name}（task 隔离）
- value：Pydantic BaseModel → model_dump_json()；dict → json.dumps
- TTL：86400s（24h，覆盖 Arq retry 窗口）
- 失败兜底：写 / 读异常 → log warning，返回 None / no-op，**不阻塞主流程**
- 关闭开关：env `STAGE_CACHE_ENABLED=false` → read/write/clear 全是 no-op
  （灰度 + 紧急回滚用 1 行 env）

调用方（pipeline.py）：
- 每步前：await read_stage(task_id, step_name) → hit 则反序列化到 ctx 并跳过
- 每步后：asyncio.create_task(write_stage(...))  fire-and-forget
- 成功后：await clear_stages(task_id)  释放 Redis
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

log = structlog.get_logger("distill.stage_cache")

# TTL 24h（覆盖 Arq retry 窗口，retry_max=2 × 任意次执行）
_DEFAULT_TTL_SECONDS = 86400

# 支持的 step 名（CP3.6.4 = 4 步 + final）
_STEP_NAMES = ("step1_structure", "step2_rewrite", "step3_tts", "step4_concat", "final")


def _is_enabled() -> bool:
    """CP3.6.4：env 关闭开关（默认开启）。"""
    return os.getenv("STAGE_CACHE_ENABLED", "true").lower() not in ("false", "0", "no")


def _key(task_id: str, step_name: str) -> str:
    """Redis key 构造（CP3.6.4：task_id + step_name 双维度）。"""
    return f"stage:{task_id}:{step_name}"


def _pattern(task_id: str) -> str:
    """清理某 task 所有 stage 的 Redis pattern（用于成功后清理）。"""
    return f"stage:{task_id}:*"


async def write_stage(task_id: str, step_name: str, output: Any) -> None:
    """写 stage cache（fire-and-forget，失败 log warning 不抛）。

    Args:
        task_id: Arq task id
        step_name: step1_structure / step2_rewrite / step3_tts / step4_concat / final
        output: Pydantic BaseModel（model_dump_json）或 dict（json.dumps）

    失败兜底：Redis 不可用 / JSON 序列化失败 → log warning，**不抛**。
    """
    if not _is_enabled():
        return
    if step_name not in _STEP_NAMES:
        log.warning("stage_cache_unknown_step", step=step_name, task_id=task_id)
        return

    try:
        # Pydantic v2 兼容
        if hasattr(output, "model_dump_json"):
            value = output.model_dump_json()
        elif isinstance(output, dict):
            value = json.dumps(output, ensure_ascii=False)
        else:
            # 兜底：尝试 str() 序列化（不推荐但总比 crash 强）
            value = json.dumps(str(output), ensure_ascii=False)
    except Exception as e:
        log.warning("stage_cache_serialize_failed", task_id=task_id, step=step_name, error=str(e))
        return

    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        await client.set(_key(task_id, step_name), value, ex=_DEFAULT_TTL_SECONDS)
    except Exception as e:
        # CP3.6.4：失败不抛（stage_cache 是优化不是正确性）
        log.warning("stage_cache_write_failed", task_id=task_id, step=step_name, error=str(e))


async def read_stage(task_id: str, step_name: str) -> Any | None:
    """读 stage cache。返回 Pydantic 模型实例 / dict / None。

    返回 None 的情况：
    - env STAGE_CACHE_ENABLED=false（关闭）
    - key 不存在（TTL 过期 / 首次跑）
    - JSON 解析失败（schema 演进 / 数据损坏）
    - Redis 不可用（降级）
    """
    if not _is_enabled():
        return None
    if step_name not in _STEP_NAMES:
        return None

    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        raw = await client.get(_key(task_id, step_name))
    except Exception as e:
        log.warning("stage_cache_read_failed", task_id=task_id, step=step_name, error=str(e))
        return None

    if raw is None:
        return None

    try:
        data = json.loads(raw)
        return data
    except (ValueError, TypeError) as e:
        # JSON 解析失败 → log warning 当 miss 处理
        log.warning("stage_cache_corrupt_json", task_id=task_id, step=step_name, error=str(e))
        return None


async def clear_stages(task_id: str) -> None:
    """清理某个 task 的所有 stage（成功后调用，释放 Redis 内存）。

    CP3.6.4：pattern scan + DEL。失败 → log warning 不抛。
    """
    if not _is_enabled():
        return

    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        # SCAN + DEL（不用 KEYS，避免阻塞 Redis）
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=_pattern(task_id), count=100)
            if keys:
                await client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        if deleted:
            log.info("stage_cache_cleared", task_id=task_id, deleted=deleted)
    except Exception as e:
        log.warning("stage_cache_clear_failed", task_id=task_id, error=str(e))
