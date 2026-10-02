"""agent 活路径 ↔ CP3.7.2 hook 框架的桥接层（2026-10-02 新增）。

## 为什么需要这个文件

生产蒸馏在 CP-AGENT-RUNNER-INTEGRATION 之后改走 LangGraph agent
（``tasks/distill_task.py`` 调 ``_run_distill_via_agent``，不再调
``DistillPipeline.run``）。而 CP3.7.2 那套 hook —— tier 路由、用户画像、
few-shot 选择、评分预测、自动重蒸、画像更新、few-shot 入池 —— 全部挂在
``pipeline.py`` 上。于是**整棵 hook 树在生产里一次都没跑过**，而代码看上去
都写好了。审计时最容易被这种「表层完整、实则死代码」骗过去。

这里把 hook 重新接回活路径，做法是：

1. agent 跑完后，用它的 final state **合成一个 DistillContext**
   （hook 的统一入参），然后逐个调用 post-hooks；
2. agent 跑之前，调用 pre-hooks 决定 target_tier / few-shot，
   把结果塞进 agent 的 initial_state。

## 为什么不能直接复用 DistillPipeline

两条路径的产出形状不一样，这是必须显式调和的地方：

- pipeline 的 Step2 产出**结构化** ``RewriteOutput{hook, sections, outro}``；
- agent 的 Step2 只产出**扁平字符串** ``rewritten_script``（整篇口播稿）。

``FewShotPoolHook`` 要的是 ``ctx.rewrite.hook``（开场钩子）。直接拿整稿去填
会被当成一个巨型 hook 塞进 few-shot 池，污染样本。所以这里按段落切分还原：
首段（≤80 字）当 hook，末段当 outro，中间当 sections。

所有 hook 失败都不破主流程 —— 蒸馏产物已经落库了，hook 挂了只该告警。
"""

from __future__ import annotations

import structlog
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .hooks_impl import default_post_hooks, default_pre_hooks
from .schemas import (
    AudioConcatOutput,
    DistillContext,
    RewriteOutput,
)

log = structlog.get_logger("distill.agent_hook_bridge")

# 方案 §1 闭环 1：hook / 节拍 / outro 各 ≤ 80 字，节拍段 80-180 字
HOOK_MAX_CHARS = 80


def _split_script(script: str) -> RewriteOutput:
    """把 agent 的扁平口播稿还原成 RewriteOutput。

    切分规则：按空行分段；首段当 hook（超长截断到 80 字），末段当 outro，
    中间段当 sections。整稿没有空行时，整篇当 hook —— 宁可 hook 长一点，
    也不要凭空造 sections 出来。
    """
    text = (script or "").strip()
    if not text:
        return RewriteOutput()

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        # 单段稿：整体当 hook，截到上限（few-shot 池的样本必须是短钩子）
        return RewriteOutput(hook=text[:HOOK_MAX_CHARS], word_count=len(text))

    hook = paragraphs[0][:HOOK_MAX_CHARS]
    outro = paragraphs[-1][:HOOK_MAX_CHARS] if len(paragraphs) > 1 else ""
    sections = paragraphs[1:-1] if len(paragraphs) > 2 else paragraphs[1:]

    return RewriteOutput(
        hook=hook,
        sections=sections,
        outro=outro,
        word_count=len(text),
    )


def build_ctx_from_agent_final(
    *,
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    raw_content: str,
    final: dict[str, Any],
) -> DistillContext:
    """用 agent 的 final state 合成 hook 用的 DistillContext。

    hook/sections/outro **优先取 agent 解析出来的结构化字段**（CP-AGENT-REWRITE-
    STRUCTURED），只在字段缺失时才回退 `_split_script` 猜。

    为什么不能一直靠猜：入池走的是 `FewShotPoolHook` → `ctx.rewrite.hook`，
    而猜的依据是「整稿第一段 = hook」。这条假设在 2026-10-02 之前是**错的** ——
    agent 输��的是扁平纯文本，LLM 带出的音效标注「（轻松开场音乐淡出）」正好
    落在第一段，于是用户给优质开场白打 4 分，系统把音效标注当高分范例存进池子
    （实测池里当时唯一一条就是这个）。

    rewrite_node 修好后拼接格式确实对齐了，「猜」也能猜对，但那是**巧合**：
    只要降级路径产出非预期段落结构，猜就会悄悄错回去，而 few-shot 池不会有
    任何告警。直接用结构化字段才是契约。
    """
    script = final.get("rewritten_script") or ""
    duration = final.get("tts_duration_sec")
    audio_url = final.get("final_audio_url") or final.get("tts_audio_url") or ""

    has_structured = any(
        final.get(k) for k in ("rewrite_hook", "rewrite_sections", "rewrite_outro")
    )
    if has_structured:
        rewrite = RewriteOutput(
            hook=final.get("rewrite_hook") or "",
            sections=list(final.get("rewrite_sections") or []),
            outro=final.get("rewrite_outro") or "",
            word_count=len(script),
        )
        # 结构化字段在但 hook 空（LLM 只给了正文）→ 退回切分至少能拿到首段
        if not rewrite.hook and script:
            rewrite = _split_script(script)
    else:
        rewrite = _split_script(script)

    ctx = DistillContext(
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        url=url,
        raw_content=raw_content or "",
        rewrite=rewrite,
    )

    if audio_url:
        ctx.final = AudioConcatOutput(
            audio_url=audio_url,
            duration_sec=int(duration) if duration is not None else 0,
        )

    # agent 不产出结构化 StructuredOutput，但 tags 是有的（step1 提取）
    tags = final.get("rewrite_tags")
    if tags:
        from .schemas import StructuredOutput

        ctx.structured = StructuredOutput(
            summary="",
            chapters=[],
            entities=[],
            tags=list(tags),
        )

    return ctx


async def run_pre_hooks(
    ctx: DistillContext,
    db: AsyncSession,
) -> DistillContext:
    """在 agent 起跑**之前**跑 pre-hooks，把决策结果写回 ctx。

    调用方负责把 ctx.target_tier / ctx.few_shot_examples 塞进 agent 的
    initial_state —— 也就是让 tier 路由和 few-shot 真正影响这一次的改写，
    而不是算完就丢。
    """
    for hook in default_pre_hooks():
        try:
            await hook(ctx, db)
        except Exception as exc:
            log.warning(
                "agent_bridge_pre_hook_failed_continue",
                hook=type(hook).__name__,
                error=str(exc),
            )
    return ctx


async def run_post_hooks(ctx: DistillContext, db: AsyncSession) -> None:
    """蒸馏成功后跑 post-hooks：评分预测 / 自动重蒸 / 画像更新 / few-shot 入池。

    单个 hook 抛错只告警不中断 —— 到这一步产物已经落库，不能因为一个
    辅助 hook 就把整次蒸馏标记成失败。
    """
    for hook in default_post_hooks():
        try:
            await hook(ctx, db)
        except Exception as exc:
            log.warning(
                "agent_bridge_post_hook_failed_continue",
                hook=type(hook).__name__,
                error=str(exc),
            )
