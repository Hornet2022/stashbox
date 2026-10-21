"""蒸馏阶段上报（ai-service 写 → content-service 读 → 客户端展示）。

## 为什么需要

蒸馏是**异步长任务**（Arq worker），端到端要几分钟：抓正文 → LLM 改写
（~30s）→ **TTS 合成（~40~120s，占大头）** → 合并。

客户端原本只能看到笼统的 `status=distilling`（安卓文案「蒸馏中」）。
用户看到「蒸馏中」停了五分钟，无从判断是在正常干活还是卡死了 ——
而实际上 TTS 合成慢是**正常现象**（上游 RTF≈0.3，1000 字稿要合成 4 分钟音频）。

这个模块把 agent 内部**已经在维护**的 `current_step`
（fetch / rewrite / tts / concat）透出来，让 App 能显示
「正在合成语音…」，把「等」变成「知道在等什么」。

## 为什么存 Redis 而不是加 DB 列

- 阶段是**临时状态**：蒸馏结束就没有意义了（终态由 articles.status 表达）
- 加 DB 列要写 alembic migration；阶段值本身不需要历史查询/统计
- 落库时机不理想：agent 是 LangGraph 图，节点边界没有 DB session，
  硬写 DB 要给每个节点注入 session_factory（改 agent 架构）
- Redis 已有 SET/GET，TTL 到期自动清理，无需 GC

阶段值取自 agent/runner.py 的 `current_step`，**不另造一套枚举**
（缝合定义：判据要能追到唯一来源）。

## 契约

- 写入方：ai-service 的 agent 节点（fetch/rewrite/tts/concat）
- 读取方：content-service 的文章详情 / 状态端点（透出 `stage` 字段）
- 生命周期：TTL 900s（15 分钟，足够覆盖最慢的蒸馏）；
  蒸馏结束/失败时主动删除（终态由 status 表达，阶段不再有意义）
- Redis 不可用时**静默降级**：不写、不抛 —— 阶段只是展示增强，
  不能因为它让整条蒸馏失败
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 阶段 key 前缀（与 cache_service 的命名风格一致）
_KEY_PREFIX = "stashbox:distill:stage:"

# 阶段值的唯一来源：agent/runner.py 的 current_step
STAGE_FETCH = "fetch"
STAGE_REWRITE = "rewrite"
STAGE_TTS = "tts"
STAGE_CONCAT = "concat"
# 唯一的例外：它描述的是**任务级**重试（Arq 重新执行整个蒸馏），
# 不是 agent 图里的某个节点。但它仍属于「蒸馏进行中」这个大语义 ——
# 客户端要能区分「在干活」和「刚才失败了一次、正在重来」。
# 不区分的话，用户在重试窗口里只能看到干巴巴的「蒸馏中」，
# 不知道发生过什么（2026-10-21 内测实测踩到过这个中间态）。
STAGE_RETRY = "retry"

_VALID_STAGES = frozenset({STAGE_FETCH, STAGE_REWRITE, STAGE_TTS, STAGE_CONCAT, STAGE_RETRY})

# 阶段 TTL（秒）。15 分钟足够覆盖最慢的蒸馏（Arq job_timeout 是 30 分钟）
_STAGE_TTL_SEC = 900


def stage_key(article_id: str) -> str:
    return f"{_KEY_PREFIX}{article_id}"


async def set_stage(article_id: str, stage: str) -> None:
    """上报蒸馏当前阶段。Redis 不可用时静默降级（不抛）。"""
    if stage not in _VALID_STAGES:
        log.debug("忽略未知阶段 stage=%s article=%s", stage, article_id)
        return
    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        try:
            await client.set(stage_key(article_id), stage, ex=_STAGE_TTL_SEC)
        finally:
            await client.aclose()
    except Exception as exc:  # 阶段只是展示增强，不能拖垮蒸馏
        log.debug("distill stage 上报失败 article=%s stage=%s err=%s", article_id, stage, exc)


async def clear_stage(article_id: str) -> None:
    """清除阶段（蒸馏结束或失败）。Redis 不可用时静默降级。"""
    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        try:
            await client.delete(stage_key(article_id))
        finally:
            await client.aclose()
    except Exception as exc:
        log.debug("distill stage 清除失败 article=%s err=%s", article_id, exc)


async def get_stage(article_id: str) -> str | None:
    """读当前阶段；没有（未开始 / 已结束 / Redis 不可用）返回 None。"""
    try:
        import redis.asyncio as redis_async

        from stashbox.backend.common.redis_client import get_redis_pool

        client = redis_async.Redis(connection_pool=get_redis_pool())
        try:
            return await client.get(stage_key(article_id))
        finally:
            await client.aclose()
    except Exception as exc:
        log.debug("distill stage 读取失败 article=%s err=%s", article_id, exc)
        return None
