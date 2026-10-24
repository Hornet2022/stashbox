"""CP-AGENT-RUNNER：听匣 agent 的 LangGraph StateGraph runner。

4 节点 + 4 边的固定 pipeline（Phase 1）：
  fetch ──▶ rewrite ──▶ tts ──▶ concat ──▶ END
                │
                └─ 任何节点失败 ──▶ failed_node ──▶ END

Phase 3（TODO）会加：conditional_edge 根据 LLM 决策动态路由。
Phase 2（当前）已经把 fetch_url / tts_synthesize 抽成 ToolRegistry 调用，rewrite
仍用 LLM 直调（避免 OpenAIClient 跟 LangGraph tool node 重复实现）。

向后兼容：
  - DistillPipeline 签名 (run + ctx) 保留 —— distill_task.py 不动也能切到 LangGraph
  - 老的 hooks（pre/post hooks）保留 —— pipeline_hooks.py 仍能被显式调用
  - 老的 stage_cache + state_machine 仍工作 —— 状态机值不变
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from observability.metrics import (
    DISTILL_ATTEMPT_TOTAL,
    DISTILL_FAILURE_TOTAL,
    DISTILL_STEP_DURATION,
    DISTILL_SUCCESS_TOTAL,
)

from . import recreation_quality
from .state import AgentState
from .tools import ToolError, get_default_registry

# CP-AGENT-LLM-IMPORT：动态模块引用而不是 `from llm import get_llm_client`，
# 让测试 monkeypatch `llm.get_llm_client` 时真正生效（`from X import Y` 会绑死
# 模块级引用，monkeypatch 改不到）。
import llm as _llm_module
from llm.exceptions import LLMError
from llm.types import ChatMessage, ChatRequest

log = logging.getLogger("agent.runner")


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


async def _report_stage(article_id: str, stage: str) -> None:
    """上报蒸馏当前阶段（供客户端展示「正在合成语音…」）。

    阶段值就是 agent 自己的 `current_step`，不另造一套枚举 ——
    客户端看到的和 agent 内部走的是同一个判据。

    写失败静默忽略：阶段只是展示增强，不能因为 Redis 抖动让整条蒸馏失败。
    """
    if not article_id:
        return
    try:
        from stashbox.backend.common.distill_stage import set_stage

        await set_stage(article_id, stage)
    except Exception as exc:  # noqa: BLE001
        log.debug("distill stage 上报失败 article=%s stage=%s err=%s", article_id, stage, exc)


def _observe_step(step_name: str) -> Callable[[Any], Any]:
    """给 agent 节点补 CP3.6 埋点：耗时 histogram + 成功/失败 counter。

    为什么需要它
    ------------
    原来只有 `observability.decorators.trace_distill_step` 埋这些点，而它只挂在
    `distill/steps.py` 的 step1~step4 上 —— 那 4 个函数只被 `DistillPipeline.run`
    调用。生产蒸馏早已改成走 LangGraph agent（distill_task 用 agent 替代
    pipeline.run），**这 4 个函数在生产一次都不执行**。

    结果就是：`distill_step_duration_seconds` 只有 HELP/TYPE、没有任何 sample，
    管理后台「蒸馏耗时」的 P50/P95/P99 永远是空。实测 ai-service /metrics 里
    同一批的 `distill_queue_size` 有值（那是 worker 直接写的），一对比就露馅。

    失败也记耗时 —— P95 统计要覆盖慢的失败请求，只记成功会低估耗时。
    """

    def _decorator(func: Any) -> Any:
        @functools.wraps(func)
        async def _wrapped(state: AgentState) -> dict[str, Any]:
            start = time.monotonic()
            DISTILL_ATTEMPT_TOTAL.labels(step=step_name).inc()
            try:
                result = await func(state)
            except Exception as exc:
                DISTILL_STEP_DURATION.labels(step=step_name).observe(time.monotonic() - start)
                DISTILL_FAILURE_TOTAL.labels(step=step_name, reason=exc.__class__.__name__).inc()
                raise
            DISTILL_STEP_DURATION.labels(step=step_name).observe(time.monotonic() - start)
            # 节点自己走 _error_update 返回时不算"成功"，按失败计
            if result.get("status") == "failed":
                DISTILL_FAILURE_TOTAL.labels(step=step_name, reason="tool_error").inc()
            else:
                DISTILL_SUCCESS_TOTAL.labels(step=step_name).inc()
            return result

        return _wrapped

    return _decorator


@_observe_step("step1_structure")
async def fetch_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-NODE-FETCH：调用 fetch_url tool，写 AgentState。

    CP-AGENT-PREFETCH：若 state 已带 `fetched_content`（distill_task 从
    articles.raw_content 预取），短路跳过工具调用 —— 避免重复抓外部网络。
    这也是生产主路径：抓取在 CP-CREATE-ARTICLE 阶段已完成并落库。
    """
    article_id = state.get("article_id", "")
    await _report_stage(article_id, "fetch")
    prefetched = state.get("fetched_content") or ""
    if prefetched.strip():
        log.info(f"agent_node_fetch_prefetched article_id={article_id} len={len(prefetched)}")
        return {"current_step": "fetch"}

    log.info(f"agent_node_fetch article_id={article_id}")
    state = {**state, "current_step": "fetch"}
    try:
        result = await get_default_registry().invoke("fetch_url", state, args={})
        return {
            "fetched_content": result.get("content", ""),
            "fetched_meta": result.get("meta", {}),
            "tool_calls": (state.get("tool_calls") or [])
            + [{"name": "fetch_url", "args": {}, "result_keys": list(result.keys()), "ts": _now()}],
        }
    except ToolError as e:
        return _error_update(state, "fetch", e)


@_observe_step("step2_rewrite")
async def rewrite_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-NODE-REWRITE：调 LLM 改写。

    Phase 1：直接调 OpenAIClient（与原 distill/steps.py:172 step2_rewrite 等价）。
    Phase 2：用 memory.inject_into_prompt 把 user_profile + few_shot 注入到 prompt。
    Phase 3：用 LangGraph 的 ToolNode 让 LLM 选 tool 决定改写风格。
    """
    log.info("agent_node_rewrite article_id=%s", state.get("article_id"))
    await _report_stage(state.get("article_id", ""), "rewrite")
    state = {**state, "current_step": "rewrite"}

    # Phase 2：从 MemoryStore 读 profile + few-shot
    user_profile = state.get("user_profile")
    few_shot_examples = state.get("few_shot_examples") or []
    fetched_content = state.get("fetched_content") or ""

    if not fetched_content:
        return _error_update(
            state,
            "rewrite",
            ToolError("badreq", "fetched_content 为空，无法改写"),
        )

    try:
        # 调原 step2（带 memory 注入）
        # 用 _llm_module.get_llm_client() 而不是 from-import，保证 monkeypatch 可生效
        # CP-AGENT-GET-LLM-SYNC：真实工厂函数是同步的（factory.py:146），
        # 不能 await —— 之前多写 await 导致生产报 "can't be used in 'await' expression"。
        llm = _llm_module.get_llm_client()

        prompt = _rewrite_prompt(fetched_content)

        # Phase 2：memory 注入（如果有 store + profile）
        if user_profile or few_shot_examples:
            # Note: MemoryStore 实例化依赖 session_factory；这里用延迟注入模式
            # —— runner 调用方注入（避免 agent 模块依赖 DB 配置）。
            prompt = _maybe_inject_memory(state, prompt)

        # ⚠️ 2026-10-21：这里曾经改成「两阶段」（分段抽素材 → 基于素材写稿），
        #    动机是「长 prompt 会触发上游丢 content」。那个动机**已被证伪** ——
        #    真正的根因是上游**非流式**路径上「增量解码 → 累积进 content」
        #    有缺陷（同一个 2925 字 prompt：非流式 4/4 空，流式 0/4 空），
        #    改成流式即可，长文一次就能改写（见 llm/openai.py:_post_stream）。
        #
        #    所以这里恢复**单次长文改写**：
        #      · 模型看到全文 → 「基于原文的关联思考/延伸判断」才有依据
        #        （分段抽素材会把稿子降级成各段摘要的拼接）
        #      · 成本 1 次调用（不是 N+1 次）
        #      · 细节不丢（不用经过「要点」这个有损中间层）
        #
        #    但**保留**了 system prompt 里新增的第 9 条（关联思考 / 延伸判断 /
        #    值得关联的关键点 / 可拓展关注的方向）—— 那是产品定位的要求，
        #    与怎么绕开上游 bug 无关。
        raw = await _chat_text(
            llm,
            prompt,
            # ⚠️ 用 replace 而不是 str.format：system prompt 里有 **JSON 示例**，
            # 里面的 `{` `}` 会被 format 当成占位符 → KeyError/IndexError。
            system_prompt=(
                _REWRITE_SYSTEM.replace(
                    "{tag_vocabulary}",
                    state.get("tag_vocabulary") or _FALLBACK_TAG_VOCABULARY,
                )
                # 素材类型差异（文档 / 代码库）—— 见 _material_guidance 的说明。
                + _material_guidance(state.get("source") or "")
            ),
            step="agent_rewrite",
            task_id=state.get("trace_id", ""),
            # ⚠️ **必须显式给 max_tokens**（2026-10-23）。
            #
            # 不给就吃默认 4096 —— 而文档稿的输出是
            # 「1000-1400 字正文 + JSON 结构 + 三类再创作」，中文 token 化后
            # 很容易顶到上限。顶到上限 = **JSON 被截断** = 解析失败
            # → 降级成纯文本按空行切 → **稿子的结构全丢**
            # （hook/sections/insights/related/follow_ups 全部糊成一坨）。
            #
            # 这个失败是**静默**的：稿子照样生成、照样能播，只是质量塌了。
            # 8192 足够一篇长文档的完整结构化输出。
            max_tokens=8192,
        )
        # 结构化解析（CP-AGENT-REWRITE-STRUCTURED）：LLM 必须回 JSON
        # {hook, sections, outro}，这样 script_text 的段落边界才是可靠的。
        parts = _parse_rewrite(raw)
        if parts.degraded:
            # ⚠️ **降级必须可观测**（2026-10-23）。
            #
            # 降级 = LLM 没按 JSON 输出 → 整稿被当纯文本按空行切 →
            # **hook/sections/三类再创作内容全部丢失**（分区是空的、稿子没有节拍）。
            # 而这个失败**完全静默**：稿子照样生成、照样能播，只是质量塌了。
            #
            # 之前没记原始输出 ⇒ 线上看到「分区全空」根本查不出是 LLM 没输出
            # 三类、还是 JSON 解析失败、还是提示词把契约带跑了。
            # 这里记前 800 字 —— 足够看出「是纯文本 / 是围栏 / 是字段名不对 / 被截断」。
            log.warning(
                "agent_rewrite_degraded raw_head=%r",
                (raw or "")[:800],
            )
        log.info(
            "agent_rewrite_parsed hook=%dB sections=%d outro=%dB fallback=%s",
            len(parts.hook),
            len(parts.sections),
            len(parts.outro),
            parts.degraded,
        )

        # ── 条目里的数字必须能在**原文**里找到（2026-10-24）────────────
        #
        # 三类再创作内容允许写「原文之外」的判断（这正是产品要的），
        # 代价是模型有机会顺手编一个**具体数字**。
        # 实测：GitHub README 的延伸判断里写了「命令数量超过 **20 个**后收益才明显」，
        # 而 README 里根本没有 20（只有 "lazy"）⇒ 听众会以为那是原文里的事实。
        #
        # 这类编造**最伤信任**：稿子其他部分都可以是「我们的看法」，
        # 但一个看起来像原文事实的数字是**事实层面的造假**。
        #
        # ⚠️ **只记不丢**（与既有的质量画像口径一致）：告警 + 抽查，
        # 不自动删内容 —— 原文被截断时会有误判（见 unsourced_numbers 的说明）。
        if not parts.degraded:
            for _name, _items in (
                ("insights", parts.insights),
                ("related", parts.related),
                ("follow_ups", parts.follow_ups),
            ):
                for _item in _items:
                    _bad = recreation_quality.unsourced_numbers(_item, fetched_content)
                    if _bad:
                        log.warning(
                            "agent_rewrite_unsourced_number group=%s numbers=%s item=%r",
                            _name,
                            ",".join(_bad),
                            _item[:200],
                        )

        if not parts.script.strip():
            return _error_update(
                state,
                "rewrite",
                ToolError("llmerr", "LLM 改写返回空内容"),
            )
        return {
            "rewritten_script": parts.script,
            # 结构化字段单独存：入池取 hook、下游要 outro 都不用再切字符串
            "rewrite_hook": parts.hook,
            "rewrite_sections": parts.sections,
            "rewrite_outro": parts.outro,
            # CP-AGENT-IS-PERSONALIZED（2026-10-02 端到端自测）：
            # 之前 `AgentState` 里压根没有这个字段，agent 全目录零处产生它，
            # 于是 distill_task.py:547 的 `bool(final.get("is_personalized"))`
            # **恒为 False**，`is_personalized` 列全表 false。报表据此说
            # 「个性化没上线」，但实际 few-shot 是真的注进 prompt 了
            # （`_maybe_inject_memory` 就在上面几行）。
            #
            # 现在如实记录「这次改写到底有没有用上个性化记忆」。注意它表达的是
            # **as-treated**（实际处理），而 ab_group 表达的是 **ITT**（分组意图）——
            # 两者不等是正常的，ab_report 对比的就是这个。
            #
            # ⚠️ 但这不代表 A/B 实验恢复有效：load_few_shots 不按 user_id 过滤，
            # 所有用户拿到的样本相同，所以两组 is_personalized 会同时为 True。
            # 真正的实验前提修复是让选样按 user_id 分组，那是另一件事。
            "is_personalized": bool(few_shot_examples)
            or bool(user_profile and user_profile.get("preferences")),
            "rewrite_quality_score": None,
            # ⚠️ 之前这里是**硬编码的空列表** —— 于是 agent 路径永远没有标签，
            # `distilled_articles.tags` 全表为空，订阅推送函数第一行就
            # `if not da.tags: return 0` → **推送一次都没产生过**
            # （生产 `push_notifications` 表实测 0 行）。
            # 现在标签由 LLM 从 tags 表的候选里选（见 _REWRITE_SYSTEM 的标签段）。
            "rewrite_tags": list(parts.tags),
            # 三类再创作内容**原样**带出（2026-10-23）。
            # 它们同时也被合并进了 rewrite_sections（TTS 整稿念）——
            # 这里多带一份，是为了让「关联思考/延伸判断/拓展方向」
            # 能**分区落库、分区展示**，而不是糊成一坨。
            "rewrite_insights": list(parts.insights),
            "rewrite_related": list(parts.related),
            "rewrite_follow_ups": list(parts.follow_ups),
            # ⚠️ **合并前**的正文要点（分区展示要用这一份）
            "rewrite_body_sections": list(parts.body_sections),
            # CP-AGENT-PASSTHROUGH：LangGraph 默认 reducer 不一定保留
            # init state 字段；显式 return 让 memory 字段在节点之间留存
            "user_profile": user_profile,
            "few_shot_examples": few_shot_examples,
        }
    except Exception as e:
        # 错误归类：CP-AGENT-LLM-ERR —— 把 httpx.TimeoutException / ConnectError
        # / 其它异常归到 ToolError(kind, message)
        if isinstance(e, ToolError):
            return _error_update(state, "rewrite", e)

        kind = "internal"
        retry_after = None

        # CP-AGENT-LLM-ERR-CLASSIFY：LLMClient 会把底层 httpx 异常包成
        # llm.exceptions.LLMError（消息形如 "...: ReadTimeout('')"），
        # 所以先按类型判 httpx 家族，再按消息关键字兜底。
        msg = str(e) or ""
        msg_low = msg.lower()

        if "readtimeout" in msg_low or "timeout" in msg_low or "timed out" in msg_low:
            kind = "timeout"
        elif "connecterror" in msg_low or "connection refused" in msg_low:
            kind = "connect"
        elif "401" in msg or "unauthorized" in msg_low or "invalid api key" in msg_low:
            kind = "auth"
        elif "429" in msg or "rate limit" in msg_low:
            kind = "ratelimit"
        elif "404" in msg or "unsupportedmodel" in msg_low:
            kind = "notfound"

        try:
            import httpx as _httpx

            if isinstance(e, _httpx.TimeoutException):
                kind = "timeout"
            elif isinstance(e, _httpx.ConnectError):
                kind = "connect"
            elif isinstance(e, _httpx.NetworkError):
                kind = "network"
            elif isinstance(e, _httpx.HTTPStatusError):
                sc = e.response.status_code
                if sc == 401:
                    kind = "auth"
                elif sc == 403:
                    kind = "forbidden"
                elif sc == 404:
                    kind = "notfound"
                elif sc == 429:
                    kind = "ratelimit"
                    retry_after = e.response.headers.get("retry-after")
                elif sc >= 500:
                    kind = "internal"
                else:
                    kind = "badreq"
        except ImportError:
            pass

        wrapped = ToolError(kind, msg or e.__class__.__name__, retry_after=retry_after)
        return _error_update(state, "rewrite", wrapped)


@_observe_step("step3_tts")
async def tts_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-NODE-TTS：调 tts_synthesize tool。"""
    log.info("agent_node_tts article_id=%s", state.get("article_id"))
    # TTS 是整条蒸馏里最慢的一步（上游 RTF≈0.3，1000 字稿要合成 4 分钟音频）。
    # 上报阶段，客户端就能显示「正在合成语音…」而不是干等「蒸馏中」。
    await _report_stage(state.get("article_id", ""), "tts")
    state = {**state, "current_step": "tts"}
    script = state.get("rewritten_script")
    if not script:
        return _error_update(
            state,
            "tts",
            ToolError("badreq", "rewritten_script 为空，无法 TTS"),
        )

    # CP-AGENT-TTS-LOOP-GUARD：禁止重复合成。
    #
    # 死循环成因（实测）：tts_synthesize 产出的 audio_url 为 None（OSS 未实现），
    # tts_node 写回 tts_audio_url=None，router 规则「有稿无音频 → skip_to_tts」
    # 于是又回到本节点 —— 每轮白合成一遍，实测一次任务合成了 3 遍 8MB+ 音频，
    # 最后靠某一块超时才收场。
    #
    # 这里显式判定：已经合成过就别再合，直接失败并说明原因。
    prev_tts = [c for c in (state.get("tool_calls") or []) if c.get("name") == "tts_synthesize"]
    if prev_tts and not state.get("tts_audio_url"):
        log.error(
            "agent_node_tts_loop_guard article_id=%s already_synthesized=%d",
            state.get("article_id"),
            len(prev_tts),
        )
        return _error_update(
            state,
            "tts",
            ToolError(
                "empty",
                f"tts_synthesize 已执行 {len(prev_tts)} 次但拿不到 audio_url"
                "（音频已落本地磁盘；请查 OSS 上传失败原因，storage/oss.py 已有真实实现）。"
                "停止重复合成 —— 上传恢复前不会再重试。",
            ),
        )

    try:
        result = await get_default_registry().invoke("tts_synthesize", state, args={})
        return {
            "tts_audio_url": result.get("audio_url"),
            "tts_audio_path": result.get("audio_path"),
            "tts_duration_sec": result.get("duration_sec"),
            # CP-TTS-VOICE：把本次用的音色带进 state，distill_task 据此回写
            # distilled_articles.tts_voice_id（None = 全局配置，来源不可溯源）
            "tts_voice_id": result.get("tts_voice_id"),
            "tts_voice_name": result.get("tts_voice_name"),
            # CP-AGENT-PASSTHROUGH：累加 tool_calls 历史
            "tool_calls": (state.get("tool_calls") or [])
            + [
                {
                    "name": "tts_synthesize",
                    "args": {},
                    "result_keys": list(result.keys()),
                    "ts": _now(),
                }
            ],
        }
    except ToolError as e:
        return _error_update(state, "tts", e)


@_observe_step("step4_concat")
async def concat_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-NODE-CONCAT：拼接音频（Phase 1 简化版：直接 done）。

    真实生产做：silence 拼接 + normalize + OSS 上传，逻辑在 distill/steps.py:695。
    Phase 1 先把骨架跑通，TODO：迁移 step4_concat 到这里。
    """
    log.info("agent_node_concat article_id=%s", state.get("article_id"))
    await _report_stage(state.get("article_id", ""), "concat")
    state = {**state, "current_step": "concat"}
    # Phase 1 占位：tts_audio_url 直接当 final_audio_url
    return {
        "final_audio_url": state.get("tts_audio_url"),
        "final_duration_sec": state.get("tts_duration_sec"),
        "current_step": "done",
        "status": "done",
        "finished_at": _now(),
    }


async def failed_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-NODE-FAILED：失败终态，透传 state.error / error_kind。"""
    log.warning(
        "agent_node_failed article_id=%s step=%s kind=%s",
        state.get("article_id"),
        state.get("error_step"),
        state.get("error_kind"),
    )
    return {
        "current_step": "failed",
        "status": "failed",
        "finished_at": _now(),
    }


# ---------------------------------------------------------------------------
# Phase 3：决策路由器（让 LLM 决定下一步走哪条边）
# ---------------------------------------------------------------------------


_DECISION_PROMPT_TEMPLATE = """你是听匣蒸馏 pipeline 的 orchestrator。当前任务卡 = `{trace_id}`，article_id = `{article_id}`。

# 当前 pipeline 状态
- fetched_content: {fetched_len} 字（{fetched_status}）
- rewritten_script: {rewrite_len} 字（{rewrite_status}）
- tts_audio_url: {tts_status}
- final_audio_url: {final_status}

# 你可以选的 next_action（必须输出 JSON，不要解释）：
- "rewrite"：fetched_content 长度足够，进入 LLM 改写
- "skip_to_tts"：rewritten_script 已生成，跳过改写直接 TTS
- "skip_to_concat"：TTS 也跳过（极短文 < 200 字不值得合成），直接 concat
- "fail"：fetched_content 空，无法继续

# 输出格式（JSON，只输出一个 key）：
{{"next_action": "rewrite", "reason": "<简述>"}}
"""


def _build_decision_prompt(state: AgentState) -> str:
    fetched = state.get("fetched_content") or ""
    rewritten = state.get("rewritten_script") or ""
    tts_url = state.get("tts_audio_url")
    final_url = state.get("final_audio_url")

    def _status(text: str | None, expected_min: int) -> tuple[str, str]:
        if text is None:
            return "缺失", "缺失"
        return f"已生成 {len(text)} 字", "OK"

    fetched_len = len(fetched)
    fetched_status = "已抓取" if fetched else "缺失"
    rewrite_len = len(rewritten)
    rewrite_status = "已改写" if rewritten else "缺失"

    return _DECISION_PROMPT_TEMPLATE.format(
        trace_id=state.get("trace_id", ""),
        article_id=state.get("article_id", ""),
        fetched_len=fetched_len,
        fetched_status=fetched_status,
        rewrite_len=rewrite_len,
        rewrite_status=rewrite_status,
        tts_status="已合成" if tts_url else "缺失",
        final_status="已拼接" if final_url else "缺失",
    )


_VALID_NEXT_ACTIONS = frozenset({"rewrite", "skip_to_tts", "skip_to_concat", "fail", "done"})

# CP-AGENT-ROUTER-MODE：router 决策来源。
#
#   rules（默认）—— 走 `_default_next_action` 的确定性规则，**不调 LLM**
#   llm           —— 每次都调 LLM 决策（Phase 3 原始行为）
#   hybrid         —— 规则可判时走规则，LLM 兜底
#
# 为什么默认 rules：`_default_next_action` 已经确定性地覆盖了**全部**状态组合
# （fetch 空→fail / 有稿无音频→skip_to_tts / 有音频无拼接→skip_to_concat /
# 全齐→done / 其余→rewrite）。也就是说在正常流程里，router 的每一次决策
# 都是规则可判的，LLM 一次都没改变过结论 —— 只是白花时间和 token
# （实测每次 router 调用约 2~5s + 约 100 completion_token，
#   整条链路要调 3~4 次 router）。
#
# 想恢复"LLM 自主决策"的实验行为，设 AGENT_ROUTER_MODE=llm 即可。
ROUTER_MODE = os.getenv("AGENT_ROUTER_MODE", "rules").strip().lower()

# CP-AGENT-ROUTER-TOKENS：router 只输出一个短 JSON（next_action + reason），
# 但默认值 max_tokens=4096 给了 reasoning 模型极大冗余。
# 实测：4096 → 4.7s / 约 100 completion_token；限到 64 → 2.3s。
ROUTER_MAX_TOKENS = int(os.getenv("AGENT_ROUTER_MAX_TOKENS", "64"))
ROUTER_TEMPERATURE = float(os.getenv("AGENT_ROUTER_TEMPERATURE", "0.0"))


async def decision_router_node(state: AgentState) -> dict[str, Any]:
    """CP-AGENT-ROUTER-NODE：决定下一步走哪条边。

    Phase 3 引入：让 LLM 根据当前 state 决策"rewrite" / "skip_to_tts" /
    "skip_to_concat" / "fail"。AgentState.next_action 字段承载决策结果，
    conditional_edges 读取它路由到不同节点。

    LLM 输出：JSON {"next_action": "<one-of>", "reason": "<text>"}

    异常处理：
      - LLM 抛异常 → 兜底走 default_next_action（基于 state 启发式）
      - LLM 输出非 JSON / next_action 不在白名单 → 兜底走 default_next_action

    CP-AGENT-ROUTER-MODE：默认 rules 模式直接用确定性规则跳过 LLM 调用
    （详见 ROUTER_MODE 注释）。
    """
    article_id = state.get("article_id", "")
    log.info(f"agent_decision_router article_id={article_id} mode={ROUTER_MODE}")
    state = {**state, "current_step": "decision"}

    prompt = _build_decision_prompt(state)

    # 启发式兜底（rule-based default_next_action）
    default = _default_next_action(state)

    if ROUTER_MODE == "rules":
        log.info(f"agent_router_rule_fastpath article_id={article_id} action={default}")
        action = default
        raw = ""
    else:
        try:
            # CP-AGENT-ROUTER-LLM：动态模块引用，让 monkeypatch 生效（测试可替换）
            # CP-AGENT-GET-LLM-SYNC：真实工厂函数是同步的（factory.py:146），
            # 不能 await —— 之前多写 await 导致生产报 "can't be used in 'await' expression"。
            llm = _llm_module.get_llm_client()
            # CP-AGENT-ROUTER-TOKENS：结构化短决策，限制 token 并降温。
            # 实测 max_tokens=4096 → 4.7s，限到 64 → 2.3s（省一半）。
            raw = await _chat_text(
                llm,
                prompt,
                system_prompt="你是蒸馏 pipeline 的 orchestrator，只输出 JSON。",
                step="agent_decision_router",
                task_id=state.get("trace_id", ""),
                max_tokens=ROUTER_MAX_TOKENS,
                temperature=ROUTER_TEMPERATURE,
            )
            action = _parse_next_action(raw)
            if action is None:
                log.warning(
                    f"agent_router_invalid_fallback article_id={article_id} raw={raw[:200]!r} fallback={default}"
                )
                action = default
        except Exception as exc:
            log.warning(
                f"agent_router_llm_error_fallback article_id={article_id} error={exc!s} fallback={default}"
            )
            # 兜底**只能**在这里。
            #
            # 原来这行 `action = default` 缩进在 try/except 之后的无条件位置，
            # 于是它每次都执行，把上面 `action = _parse_next_action(raw)` 的
            # LLM 解析结果直接覆盖掉 —— `AGENT_ROUTER_MODE=llm` 变成
            # "花 2~5s 和真实 token 换一个必然被丢弃的结果"。
            # 默认 `rules` 走不到这个分支，所以这个缺陷一直潜伏着。
            action = default

    # CP-AGENT-ROUTER-FINALIZE：action="done" 时直接 finalize state（status="done"）
    # 不依赖后续 concat_node 设置 status。routing 把 action=done 路由到 __end__，
    # LangGraph 把 router return dict 合并到 final state，所以这里必须写 status。
    finalize: dict[str, Any] = {}
    if action == "done":
        finalize = {
            "status": "done",
            "current_step": "done",
            "finished_at": _now(),
        }
    elif action == "fail":
        finalize = {
            "status": "failed",
            "current_step": "failed",
            "finished_at": _now(),
        }
    else:
        finalize = {"current_step": "decision"}

    # CP-AGENT-ROUTER-NO-TOOL-CALL：router 自己不算"工具调用"，它只是决策。
    # tool_calls 历史只记真正调用的工具（fetch_url / tts_synthesize）。
    return {
        "next_action": action,
        **finalize,
    }


def _default_next_action(state: AgentState) -> str:
    """CP-AGENT-ROUTER-DEFAULT：LLM 挂掉/输出非法时的兜底。

    启发式：
      - fetched 空 → fail
      - 已 rewrite 但没 TTS → skip_to_tts
      - 已 TTS 但没 concat → skip_to_concat
      - 全完成（fetched+rewritten+tts+final 都有） → done
      - 已 fetch 但没 rewrite → rewrite
    """
    if not state.get("fetched_content"):
        return "fail"
    if state.get("rewritten_script") and not state.get("tts_audio_url"):
        return "skip_to_tts"
    if state.get("tts_audio_url") and not state.get("final_audio_url"):
        return "skip_to_concat"
    # CP-AGENT-ROUTER-DONE：全完成 → done（避免无限循环）
    if state.get("rewritten_script") and state.get("final_audio_url"):
        return "done"
    return "rewrite"


def _parse_next_action(raw: str) -> str | None:
    """CP-AGENT-ROUTER-PARSE：从 LLM 输出里解析 next_action。"""
    import json as _json
    import re

    if not raw:
        return None
    # 尝试直接 JSON parse
    try:
        obj = _json.loads(raw.strip())
        if isinstance(obj, dict) and "next_action" in obj:
            v = str(obj["next_action"]).strip().lower()
            if v in _VALID_NEXT_ACTIONS:
                return v
    except Exception:
        pass
    # 退化：从字符串里搜 "next_action": "xxx"
    m = re.search(r'"next_action"\s*:\s*"(\w+)"', raw)
    if m:
        v = m.group(1).lower()
        if v in _VALID_NEXT_ACTIONS:
            return v
    # 退化：搜首段 "rewrite" / "skip_to_tts" / "skip_to_concat" / "fail"
    raw_low = raw.strip().lower()
    for token in ("skip_to_concat", "skip_to_tts", "rewrite", "fail"):
        if token in raw_low:
            return token
    return None


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _error_update(state: AgentState, step: str, err: ToolError) -> dict[str, Any]:
    return {
        "current_step": "failed",
        "status": "failed",
        "error": err.message,
        "error_kind": err.kind,
        "error_step": step,
        "retry_after": err.retry_after,
        "finished_at": _now(),
    }


# ---------------------------------------------------------------------------
# 改写：结构化输出契约（CP-AGENT-REWRITE-STRUCTURED）
# ---------------------------------------------------------------------------
#
# 为什么必须结构化（2026-10-02 端到端自测挖出）：
#
#   下游 `evaluation_service.submit_user_evaluation` 用
#   `script_text.split("\n\n", 1)[0]` 取 hook 入 few-shot 池，
#   这条假设只在「script_text = hook \n\n sections \n\n outro」时成立。
#
#   但 agent 的 prompt 曾经是一句「改写成听感稿」，让 LLM 自由输出纯文本。
#   LLM 于是照着原文风格带出了音效标注，script_text 变成：
#
#       （轻松开场音乐淡出）
#
#       哈喽各位正在通勤路上的朋友，今天咱们来聊聊…
#
#   `split("\n\n")[0]` 拿到的就是那行音效标注。于是**用户给优质开场白打了
#   4 分，系统把音效标注当「高分改写范例」存进池子**喂给后续所有改写。
#   实测池里唯一一条就是 `（轻松开场音乐淡出）`。
#
#   旧的 `distill/prompts.py:STEP2_SYSTEM` 本来就定义了 JSON 契约
#   （hook/sections/outro/word_count），是 agent 换 prompt 时把这个契约丢了。
#   这里把它捡回来，并保留容错：LLM 不听话时降级成「整段当正文」，
#   宁可 hook 语义不完美，也不能让音效标注被当成范例。


_REWRITE_SYSTEM = """你是一个中文播客主理人。任务：把原文改写成适合通勤收听的播客稿
（目标听众：上下班通勤、希望快速吃透一篇文章要点的人）。

【输出格式 — 严格 JSON，不要 Markdown 代码块】
{
  "hook": "开场钩子，≤ 80 字，15 秒左右能读完",
  "sections": ["核心要点（正文节拍），一节一个字符串，80-160 字，共 3-5 节"],
  "related": ["关联思考，1-2 条，每条 45-80 字"],
  "insights": ["延伸判断，2-3 条，每条 50-100 字"],
  "follow_ups": ["拓展方向，1-2 条，每条 45-80 字"],
  "outro": "收束，≤ 80 字，呼应 hook 的开头句式或意象",
  "tags": ["主题标签，1-3 个，必须从候选标签里原样选"]
}

【主题标签 — 必须从候选标签里原样选】
候选标签：{tag_vocabulary}

规则：
1. **只能从上面的候选标签里挑**，逐字照抄，不要改写、不要新造、不要加后缀。
   订阅推送是**按标签名精确匹配**的 —— 写成「科技资讯」「财经类」这类
   近似词，订阅了这个标签的用户就**永远收不到**这篇的推送。
2. 选 1~3 个真正切题的；都不贴切就别选（空数组也是合法答案）。
3. 标签**不参与播报**，只是给订阅用的检索键 —— 不要在正文里念标签。

【写作原则】
1. **信息密度**：每句要么给新事实，要么给新视角。删掉"咱们一起来看看""接下来要说的是"这类水词
2. **口语化但不失准**：用"咱们""有意思的是""其实啊"等口语连接词；数字/机构/人名必须与原文一致
3. **钩子要狠**：hook 必须在前 15 秒让人想听完。可以是反问、矛盾、或出人意料的对比
4. **节拍过渡**：sections 各节靠自然语义承接，不要写"接下来我们看看""说到这里"这种过渡套话
5. **收束呼应**：outro 呼应 hook 的开头意象，让人感觉绕了一圈回来了
6. **不要复读原文**，要重述 + 加你的解读
7. **不要输出音效标注**：不要出现"（轻松开场音乐淡出）""【片头音乐】"这类
   舞台提示或音效描述。它们会被当成正文混进稿子污染下游。
8. **不要 markdown、不要 bullet、不要 emoji、不要"听众朋友们"这种播音腔**
9. **这是再创作，不是摘要**：播客稿 = **核心要点（sections）+ 关联思考（related）
   + 延伸判断（insights）+ 拓展方向（follow_ups）**。后三类是稿子里
   **原文之外**的部分，是这个产品和「摘要」的区别所在。

   ⚠️ **四段的顺序就是这个顺序**（核心要点 → 关联思考 → 延伸判断 → 拓展方向）——
   稿子照这个顺序念，App 的分区也照这个顺序显示。

   ⚠️ **四段都是稿子的结构，不是可选项**：核心要点 + 关联思考 + 延伸判断
   + 拓展方向，**缺一段这稿子就不完整**。宁可某一条写得浅一点，
   也不要整段留空 ——「宁缺毋滥」约束的是**单条的质量**（没推理 / 复述原文
   / 名词短语），不是「这段干脆不写」。

   ⚠️ 三个字段各管一件事，不要混：
     · related（关联思考）—— 这件事和**什么**相关/矛盾/互相印证？
       别的公司、别的行业、别的技术路线、别的产品形态
     · insights（延伸判断）—— **往后看会怎样**？原文没写、但能推出的
       真正的瓶颈、代价、边界、什么时候会失效
     · follow_ups（拓展方向）—— 听众接下来该**盯住什么**，为什么现在值得盯
     （顺序与上面第 9 条一致：核心要点 → 关联思考 → 延伸判断 → 拓展方向）

   这三个字段是稿子的组成部分，会被念给听众听，所以每一条都要满足下面三条硬要求：

   ① **有推论**：必须写出"因为/所以/但/反而/这意味着/前提是…"这类推理，
      让人知道你**为什么**这么判断。只给定义、只给标签、只复述原文主旨，
      一律不合格。
   ② **有依据**：依据 = **原文给了什么事实/矛盾/数字** **加上你自己的领域判断**。
      ⚠️ 延伸判断本来就不在原文明说 —— 否则它就不是"延伸"了。
      所以「有依据」**不是**要求「原文明说了这句话」，而是要求
      「能从原文的事实推出它，且推得站得住」。
      但**不许编造具体事实**（数字、版本号、事件、时间），那是另一条硬要求。
      **推不出就换一条写，不要整段留空** —— 四个字段是稿子的**结构**，
      不是可选项（见下面第 9 条开头的说明）。
   ③ **是给听众的一句话判断**，不是标签、不是选题清单、不是短语。
      听众只听声音、看不到列表，所以每条都要能独立站住。

   **浅 vs 深的对照**（左边是生产上真实产出的不合格样本，右边是合格写法）：

   ✗ 错：「模型能力与执行基建的脱节是散户最大痛点，
          未来竞争焦点将从'谁模型强'转向'谁数据管道更干净'」
          （原文已经说过这句 = 复述，不是延伸；而且只是换个说法）
   ✓ 对：「散户真正被卡住的地方不是模型选得对不对，
          而是有没有一套能每天跑回测的环境——
          这意味着模型换代对散户几乎没有帮助，除非你先把基建补上。」

   ✗ 错：「Bloomberg终端年费近3万美元，QuantGPT本质是AI时代的Bloomberg接口」
          （只说"是什么"，不说"所以呢"；这是定义，不是关联思考）
   ✓ 对：「Bloomberg靠三万美元年费把数据、终端和分析师锁在一起，
          QuantGPT想复制的是这套组合，但价格压到个人能接受——
          如果它真能做到，散户第一次能摸到机构级的研究管线。」

   ✗ 错：「Alpaca作为券商API层，是AI交易从研究到执行的关键信任锚点」
          （定义式标签，念出来只是名词解释）
   ✓ 对：「Alpaca在这套架构里承担的是"钱不离开券商"这条底线，
          这比策略准不准更要紧——因为前者是合规问题，后者只是收益率问题。」

   ✗ 错：「MCP协议在交易场景下的安全边界设计，如何防止AI被提示注入后误操作」
          （名词短语 = 选题标签，不是给听众的话）
   ✓ 对：「MCP接进交易场景后最该盯的是提示注入：
          外部内容一旦能改写策略参数，AI的'聪明'就变成了风险，
          所以权限必须收回人能确认的那一环。」

   ✗ 错：「幸存者偏差对量化策略回测的量化影响，
          以及公开数据源中退市公司覆盖率的差异」
          （两个名词短语拼在一起，仍然是标签）
   ✓ 对：「很多回测漂亮得不真实，根子常在数据里没有退市公司：
          幸存者偏差会把"买对的股票"算成"选股能力强"，
          所以判断一个策略，先看它吃没吃过退市的账。」

   **分字段的额外要求**：
     · related（关联思考）—— 不只要说清"是什么"，还要说清它和原文论点
       **的关系**：印证？矛盾？提供反例？还是提供一条不同的路径？
     · insights（延伸判断）—— 必须是**原文没有明说**的判断：往后看会怎样、
       真正的代价是什么、什么时候会失效。写之前自问：听众读完原文能不能
       直接得出这句话？能，就换一条。
       ⚠️ 「原文没有明说」**不是**「推不出来」—— 延伸判断的依据是
       **原文的事实 + 一般规律**，所以它本来就不该在原文明说。
       **这一段不允许整段留空。**
     · follow_ups（拓展方向）—— 必须是**给听众的判断句**：盯住什么、
       为什么现在值得盯。不许写成"关注X""研究Y"这种选题清单。
     · **四段都要有**：实在写不出来就写得浅一点、写得保守一点，
       但**不要整段留空** ——「宁缺毋滥」约束的是**单条的质量**
       （没推理 / 复述原文 / 名词短语），不是「这段干脆不写」。
10. **长度自适应**：原文 < 500 字 → 600-800 字；500-2000 字 → 800-1100 字；> 2000 字 → 1000-1400 字
    （sections + insights + related + follow_ups 合计）

输出纯 JSON。"""


#: 各类素材的**写作口径差异**（按 source 追加到 system prompt 末尾）。
#:
#: ## 为什么是「追加一段」而不是维护两套 prompt
#:
#: 公众号文章 / 技术文档 / 代码库 README 的**共同要求**占了绝大多数
#: （JSON 契约、三类再创作的硬要求、标签从候选里选、长度自适应…）。
#: 维护两套 prompt 意味着那些共同要求会被改一处漏一处 —— 而漏的地方
#: 恰恰是最要紧的（JSON 契约一变，解析就降级成纯文本）。
#:
#: ⇒ 共同部分留在 `_REWRITE_SYSTEM`，**只有真正不同的部分**进这里。
#:
#: ## 每条差异都必须有「不这样会怎样」
#:
#: 凭空加的规则会稀释 LLM 的注意力。下面每条都标了「不这样会怎样」。
_MATERIAL_KIND_BY_SOURCE: dict[str, str] = {
    "document": "document",
    "pdf": "document",  # PDF 直链抓取 —— 本质也是文档
    "github": "github",  # 开源库 README
}


def _material_guidance(source: str) -> str:
    """按素材类型返回**追加段**（空串 = 默认口径，即公众号文章）。

    ⚠️ 判定依据是 `raw_content["source"]`（真实来源），
    **不是** `articles.source`（那是请求体里传的，默认 "web"，会判错）。
    """
    kind = _MATERIAL_KIND_BY_SOURCE.get(source or "")
    if kind == "document":
        return _DOCUMENT_MATERIAL_GUIDANCE
    if kind == "github":
        return _GITHUB_MATERIAL_GUIDANCE
    return ""


#: ⚠️ 2026-10-24 产品拍板后，本段第 7 条**改过口径**（历史留在这里，不要再改回去）：
#:
#: 原文写的是「insights（延伸判断）在文档上常常不成立，文档本身没给依据，
#: 没有依据就给空数组，不要凑数」——
#: 而产品明确裁决：「**稿子不该是原文的压缩**，而该是
#: **原文核心要点 + 关联思考 + 延伸判断 + 拓展方向**」。
#:
#: ⇒ 四段是稿子的**结构**，缺一段就不完整，不能因为「原文没给依据」而整段放弃。
#: 「延伸判断本来就不该由原文明说」—— 它的依据是**原文的事实 + 一般规律**。
#:
#: 教训：这段口径是我自己加的分支（不是产品要求），它在**两个新来源上
#: 稳定产出 insights=0**，而这个失败**完全静默**（空组被质量检查跳过）。
#: ⇒ 教训见 `recreation_quality.missing_parts`：缺段必须可检测。
_DOCUMENT_MATERIAL_GUIDANCE = """

【素材类型：技术文档 / 报告（不是公众号文章）】
原文是**一份文档**，不是一篇有叙事线公众号文章。下面几条与上面的默认口径不同，
**按这里的来**：

1. **hook 讲「这份文档解决什么问题」，不讲「你有多久没听 Podcast 了」**。
   文档没有悬念，硬造悬念 = **编造**。写不出钩子就别写 hook（空串合法）。
2. **outro 讲「什么时候该翻回原文查证」，不要强行呼应意象**。
   文档没有意象可呼应；硬造 = 编造。
3. **「不要复读原文」这条要放宽**：文档的价值恰恰是**准确**。
   改成「**复述要准确**（数字/版本号/API 名/命令一个字都不能错），
   但要在准确之上加你的判断，而不是把原文换个说法」。
4. **sections 按文档的章节/主题组织**，不要为了凑「3-5 节」把不相干的部分
   硬凑成节。文档有 8 个主题就写 8 节。**允许超出 3-5 节**。
5. **长度自适应放宽**：文档常有上万字，默认的「>2000 字 → 1000-1400 字」
   会逼 LLM **漏掉要点**。改成「覆盖文档的**主要章节**，每个都点到；
   篇幅由文档实际结构决定，但**不要为了凑字数注水**」。
6. **不要念代码块 / 命令 / 路径 / 配置片段的原文**。它们会被 TTS 念成
   「派 pip install」，听众完全听不懂。要提到就用一句话说清它干什么
   （例如「项目要求 Python 3.11 以上」），**不要逐字念**。
   公式/表格同理：说结论，不念符号。
7. **insights（延伸判断）在文档上同样成立，照常写**。
   文档确实很少明说「往后会怎样」，但**延伸判断本来就不该由原文明说** ——
   它的依据是「**文档给了什么事实** + **一般规律**」。
   文档天然隐含着一个**适用前提**（它解决什么问题、代价是什么、
   在什么条件下就不成立了），那就是延伸判断的落点。
   例（结构，不是文风示范）：文档讲「用重试预算限制重试次数」——
   延伸判断就写「**重试预算能压住单点故障，但挡不住下游整体变慢**，
   因为那时候重试的不是失败而是排队，所以预算表看着正常、系统照样超时」。
   ⚠️ 不许编造具体事实（数字、版本、事件）—— 那是另一条硬要求。
8. **tags 只能从候选标签里逐字选**。文档主题（Python / 数据库 / 前端…）
   大概率不在候选里 —— 不在就**少选或空数组**，
   **不要自己造**（造了订阅推送永远匹配不上，等于白写）。
"""


_GITHUB_MATERIAL_GUIDANCE = """

【素材类型：GitHub 开源库的 README】
原文是一个**开源库的 README**（外加仓库元信息：描述 / star 数 / 语言 / 许可证）。
读者是「想知道这个库能干什么、值不值得用、跟别的库怎么比」的通勤者。

1. **hook 讲「这个库解决什么问题」，不要讲 Podcast 本身**。
   README 没有悬念，硬造悬念 = 编造。写不出钩子就留空（空串合法）。
2. **sections 按「这个库能做什么 / 怎么用 / 适合谁 / 有什么坑」组织**，
   而不是照抄 README 的章节标题。
3. **related（关联思考）是这类素材的主力**：它和什么同类项目可比、
   替代方案是什么、和另一个库的关键差异在哪。**这条在开源库上非常成立，
   优先写好它。**
4. **insights（延伸判断）照常写**。
   README 天然隐含着**边界与代价**：它解决什么、代价是什么、
   在什么规模/什么前提下就不划算了 —— 那就是延伸判断的落点。
   例（结构，不是文风示范）：README 说「零依赖、跨平台」——
   延伸判断就写「**零依赖是省事，但代价是插件生态得自己维护**，
   所以它适合不想背依赖树的项目，一旦要接数据库就得重新算一遍成本」。
   ⚠️ 不许编造具体事实（star 数、版本号、某项目的现状）—— 数字只能照抄原文。
5. **follow_ups（拓展方向）**：这个库接下来值得关注什么（版本节奏、
   维护者活跃度、生态动向）—— README 给不出就别写。
6. **命令 / 安装步骤 / 代码片段不要逐字念**。用一句话说清它做什么
   （例如「一条命令装好，Python 3.9 以上可用」），不要念
   `pip install requests`。
7. **数字要准**：版本号、star 数、依赖要求都是原文里的，照抄，
   不要四舍五入或夸大。
8. **tags 只能从候选标签里逐字选**。代码库主题（Python / 数据库 / Web 框架…）
   大概率**不在**候选里 —— 不在就**少选或空数组**，
   **绝对不要自己造**（造出来的标签订阅推送永远匹配不上，等于白写）。
"""


@dataclass
class _RewriteParts:
    """解析后的改写结果。

    `script` 是给 TTS / 落库用的整稿；`hook` / `sections` / `outro` 是结构化原样。
    `degraded=True` 表示 LLM 没按 JSON 输出，已降级成纯文本。
    """

    hook: str = ""
    sections: list[str] = field(default_factory=list)
    outro: str = ""
    degraded: bool = False
    #: 主题标签（订阅推送的检索键）。LLM 从 tags 表的候选里选，逐字照抄。
    tags: list[str] = field(default_factory=list)
    #: 三类再创作内容**各自的原文**（2026-10-23）。
    #:
    #: 为什么单独存：它们在改写 JSON 里是三个独立字段，但解析时被
    #: **合并进 sections**（`extra = related + insights + follow_ups`，顺序 = 产品定义）——
    #: 于是下游**彻底分不清**：「关联思考」和「正文要点」变成同一堆段落，
    #: 既不能分区展示，也不能被程序化消费（API 里只有一坨 `script_text`）。
    #:
    #: 保留合并行为（TTS 整稿念、下游 script_text 全部不变），
    #: 只是**另外**把三栏原样带出来 → 落库进 `content_parts` → App 能分区显示。
    insights: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    #: **合并前**的正文要点（不含上面三类）。分区展示要用这一份。
    body_sections: list[str] = field(default_factory=list)

    @property
    def script(self) -> str:
        """拼整稿，**段落边界由我们控制**，不交给 LLM。

        每段内部先把连续空行压掉 —— 只要 hook 内部没有空行，
        `split("\n\n", 1)[0]` 拿到的就一定是 hook 本身。
        """
        parts = [self.hook, *self.sections, self.outro]
        return "\n\n".join(_flatten_para(p) for p in parts if p.strip())


# 音效/舞台提示：整段就是括号或方括号包着的一小段，且不含句号
_SFX_RE = re.compile(r"^[\(（\[【][^\n]{0,24}[\)）\]】]\s*$")


#: 标签候选不可用时的兜底文案（与 pipeline 的 tag_vocab_cache 保持一致口径）。
_FALLBACK_TAG_VOCABULARY = "（候选标签暂不可用，本次不输出 tags）"


def _rewrite_prompt(fetched_content: str) -> str:
    """构造改写 prompt（user 侧）。

    system 契约放在 `_chat_text(system_prompt=...)`，这里只拼原文。
    """
    return f"# 原文\n{fetched_content}"


def _str_list(value: Any) -> list[str]:
    """把 LLM 给的「可能是数组/可能是字符串/可能是 None」统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [_flatten_para(value)] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            s = str(item).strip()
            if s:
                out.append(_flatten_para(s))
        return out
    return []


#: 阶段 ② 除了素材包，还能带上原文全文的原文长度上限。
#:
#: 带上原文是为了**不丢细节**（素材是压缩过的）；但带上原文就等于回到
#: 「长 prompt」的老问题 —— 所以只在原文确实短时才带。
#: 实测 user 735 字 → 50% 空，所以这个上限取 600（留一半余量）。
_REWRITE_WITH_SOURCE_MAX = 600


def _flatten_para(text: str) -> str:
    """把一段内部的连续换行压成单换行，保证它整体还是一个「段」。"""
    return re.sub(r"\n{2,}", "\n", (text or "").strip())


def _is_sfx_para(text: str) -> bool:
    """整段是否是音效标注（而不是正文）。

    只认「整段被括号包住且很短」这一种明确形态 —— 宁可漏判也不要误杀
    正常正文，正文误杀进 hook 的代价（污染池子）比分段不完美大得多。
    """
    t = (text or "").strip()
    return bool(t) and bool(_SFX_RE.match(t))


def _extract_json_object(raw: str) -> dict | None:
    """从 LLM 输出里抠出 JSON 对象。

    容错三种常见不听话：```json 围栏、前后有解释文字、尾部多余逗号。
    """
    text = (raw or "").strip()
    if not text:
        return None
    # 1. 剥 markdown 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 2. 抠第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    # 3. 容忍尾随逗号
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_rewrite(raw: str) -> _RewriteParts:
    """解析 LLM 改写输出，降级但不崩。

    降级路径（`degraded=True`）：整段当正文，并**主动跳过开头的音效段** ——
    这正是 2026-10-02 那条污染样本的形态，宁可 hook 为空也不能让
    「（轻松开场音乐淡出）」进池子。

    ⚠️ 额外字段（insights / related / follow_ups）会被**合进 sections**，
    不是丢掉 —— 播客稿是「基于原文的再创作」，这三类内容（关联思考/延伸判断、
    值得关联的关键点、可拓展关注的方向）是稿子的组成部分，要被念出来。
    """
    obj = _extract_json_object(raw)
    if obj is not None:
        hook = _flatten_para(str(obj.get("hook") or ""))
        raw_sections = obj.get("sections") or []
        if isinstance(raw_sections, str):
            raw_sections = [raw_sections]
        sections = [_flatten_para(str(s)) for s in raw_sections if str(s).strip()]
        outro = _flatten_para(str(obj.get("outro") or ""))

        # 额外字段 → 合进 sections（顺序：正文要点 → 关联思考 → 延伸判断 → 拓展方向）
        insights = [_flatten_para(s) for s in _str_list(obj.get("insights")) if s.strip()]
        related = [_flatten_para(s) for s in _str_list(obj.get("related")) if s.strip()]
        follow_ups = [_flatten_para(s) for s in _str_list(obj.get("follow_ups")) if s.strip()]
        n_insights = len(insights)
        n_related = len(related)
        n_follow = len(follow_ups)
        # ⚠️ 合并顺序 = 产品定义的顺序（核心要点 → 关联思考 → 延伸判断 → 拓展方向），
        # 稿子照这个顺序念、App 的分区也照这个顺序显示 —— 两边必须一致。
        extra = related + insights + follow_ups
        # ⚠️ **合并前**的正文要点（2026-10-23）。
        #
        # 合并之后 sections 里就混进了三类再创作内容，下游再也分不清
        # 「哪段是正文要点、哪段是关联思考」—— 存进 content_parts 的
        # 必须是**合并前**的这一份，否则分区只是换了个写法的同一坨。
        body_sections = list(sections)
        if extra:
            sections = sections + extra
        # 结构化计数（比关键词匹配准 —— 关键词会低估：LLM 写的是
        # 「这条和XX行业很像」而不是「有意思的是」）
        log.info(
            "agent_rewrite_recreation insights=%d related=%d follow_ups=%d merged=%d sections=%d",
            n_insights,
            n_related,
            n_follow,
            len(extra),
            len(sections),
        )

        # ── 再创作内容的质量画像 ────────────────────────────────────
        #
        # 「关联思考/延伸判断/拓展方向」最容易写成**标签**（选题短语、定义、
        # 或者把原文主旨换个说法复述一遍）—— 内测反馈「不够专业、深度」。
        #
        # 这里按可机械判定的契约出画像（见 agent/recreation_quality.py）：
        # 无句末标点 / 祈使句开头 / 名词化选题 / 复述正文。
        # **只记不丢** —— 丢掉会让「再创作」内容缺失（比浅更糟）。
        body_text = "\n".join([hook] + sections)
        quality = recreation_quality.summarize(
            {"insights": insights, "related": related, "follow_ups": follow_ups},
            body_text,
        )

        # ── 四段齐备（2026-10-24 产品拍板）───────────────────────────
        #
        # 产品的定义：「稿子不该是原文的压缩，而该是
        # **原文核心要点 + 关联思考 + 延伸判断 + 拓展方向**」。
        # ⇒ 缺任何一段都是**可检测的缺陷**，不是「内容质量」问题。
        #
        # ⚠️ 这条检查以前**不存在**，而空组被上面的 `if not q["total"]: continue`
        # **直接跳过** ⇒ 「延伸判断整段为 0」在日志里完全静默。
        # 实测：文档剪藏与 GitHub 剪藏产出的稿子 insights 都是 0，
        # 看上去「挺完整」—— 少了四分之一的内容，没人发现。
        missing = recreation_quality.missing_parts(
            {
                "sections": body_sections,
                "insights": insights,
                "related": related,
                "follow_ups": follow_ups,
            }
        )
        if missing:
            # ⚠️ 用 warning 而不是 info：缺段是需要有人看的信号，
            # info 级别在生产的日志量里会被淹掉。
            log.warning(
                "agent_rewrite_recreation_missing_part parts=%s "
                "(稿子结构缺段：核心要点/关联思考/延伸判断/拓展方向)",
                ",".join(missing),
            )

        for name, q in quality.items():
            if not q["total"]:
                continue
            log.info(
                "agent_rewrite_recreation_quality group=%s total=%d ok=%d issues=%s",
                name,
                q["total"],
                q["ok"],
                q["issues"] or "{}",
            )
            for bad in q["offenders"]:
                # ⚠️ 别只打 80 字：实测里「不在A，而在B」这种对比论证的
                # 推理词就在第 80 字之后，截断之后**根本没法判断是真不合格
                # 还是误判**（只能靠猜）。
                log.warning(
                    "agent_rewrite_recreation_weak_item group=%s item=%r",
                    name,
                    bad[:200],
                )

        # hook 缺失但有正文 → 拿第一节顶上，别让整稿没有钩子
        if not hook and sections:
            hook, sections = sections[0], sections[1:]

        # ── 主题标签（订阅推送的检索键）────────────────────────────────
        #
        # ⚠️ 必须**逐字**保留 LLM 给的标签名：推送是按
        # `Tag.name.in_(da.tags)` 精确匹配的（distill_task.py:143），
        # 近似词（「科技资讯」而不是「科技」）会让订阅者永远收不到。
        # 所以这里**不做任何归一化**，清洗放到落库那一层（那里能查 tags 表）。
        tags = [str(t).strip() for t in _str_list(obj.get("tags")) if str(t).strip()][:3]
        if tags:
            log.info("agent_rewrite_tags tags=%s", tags)

        if hook or sections or outro:
            return _RewriteParts(
                hook=hook,
                sections=sections,
                outro=outro,
                tags=tags,
                # 三类再创作内容原样带出（合并进 sections 的行为不变）
                insights=insights,
                related=related,
                follow_ups=follow_ups,
                body_sections=body_sections,
            )

    # 降级：按空行切段，跳掉开头的音效标注段
    paras = [_flatten_para(p) for p in (raw or "").split("\n\n")]
    paras = [p for p in paras if p]
    while paras and _is_sfx_para(paras[0]):
        paras.pop(0)
    if not paras:
        return _RewriteParts(hook="", sections=[], outro="", degraded=True)
    return _RewriteParts(
        hook=paras[0],
        sections=paras[1:-1] if len(paras) > 2 else paras[1:],
        outro=paras[-1] if len(paras) > 1 else "",
        degraded=True,
    )


# ---------------------------------------------------------------------------
# 记忆注入
# ---------------------------------------------------------------------------


def _maybe_inject_memory(state: AgentState, base_prompt: str) -> str:
    """CP-AGENT-MEMORY-INJECT-FROM-STATE：从 state 注入 memory。"""
    profile = state.get("user_profile")
    examples = state.get("few_shot_examples") or []
    if not profile and not examples:
        return base_prompt
    from .memory import FewShotExample, UserProfile, MemoryStore

    p = None
    if profile:
        p = UserProfile(
            user_id=profile.get("user_id", 0),
            tier=profile.get("tier", "free"),
            ab_group=profile.get("ab_group"),
            preferences=profile.get("preferences", {}),
        )
    e = [FewShotExample(**ex) if isinstance(ex, dict) else ex for ex in examples]
    store = MemoryStore()
    return store.inject_into_prompt(p, e, base_prompt)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _chat_text(
    llm: Any,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    step: str,
    task_id: str = "",
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """CP-AGENT-LLM-CHAT：统一 LLM 调用入口。

    真实 LLMClient 接口是 `chat(ChatRequest) -> ChatResponse`（见 llm/types.py），
    不是 `chat(str) -> str`。本 helper 负责构造 ChatRequest / 取 resp.content，
    避免 agent 各处直接拼请求。

    system_prompt=None 时只发 user 消息（满足 step2 改写等单轮场景）。

    CP-AGENT-CHAT-TOKENS：max_tokens / temperature 必须由调用方显式给出。
    之前两边都不传 → 一律吃默认值 max_tokens=4096 / temperature=0.7。对
    router 这种只需输出 `{"next_action": "rewrite", "reason": "..."}` 的
    结构化决策，4096 是巨大冗余（实测 4.7s → 限到 64 后 2.3s）。
    不传就用默认值，保持老调用点行为不变。
    """
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append(ChatMessage(role="system", content=system_prompt))
    messages.append(ChatMessage(role="user", content=user_prompt))

    kwargs: dict[str, Any] = {"metadata": {"task_id": task_id, "step": step}}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    req = ChatRequest(messages=messages, **kwargs)
    resp = await llm.chat(req)
    # ChatResponse.content 是 str；防御：万一测试 mock 直接返回 str
    #
    # ⚠️ 这里**不能**写成 `getattr(resp, "content", None) or str(resp)`：
    #    content 为空串时，str(resp) 会把**整个响应对象的 repr** 当成正文返回
    #    （"content='' model='Qwen3.8-27B' usage=Usage(...) latency_ms=..."），
    #    于是「听感整理稿」里出现这种天书，而文章状态还是"已就绪"、
    #    音频照常合成 —— 静默到没法从界面上发现（2026-10-21 实测踩过）。
    #
    #    正确做法：只接受真正的文本；content 为空就抛错，让上层走
    #    "LLM 返回空内容"的错误路径，而不是把调试信息当稿子念出去。
    if isinstance(resp, str):
        return resp
    content = getattr(resp, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    raise LLMError("LLM 返回空内容（content 为空，重试后仍为空）")


# ---------------------------------------------------------------------------
# 边决策（Phase 1：固定 4 步；Phase 3：改 conditional_edge + LLM router）
# ---------------------------------------------------------------------------


def _route_after_fetch(state: AgentState) -> str:
    """CP-AGENT-ROUTE-FETCH：fetch 后必走 decision_router（Phase 3）。"""
    if state.get("current_step") == "failed":
        return "failed_node"
    return "decision_router_node"


def _route_after_decision_router(state: AgentState) -> str:
    """CP-AGENT-ROUTE-DECISION：根据 LLM 决策的 next_action 路由。

    next_action → node:
      "rewrite"        → rewrite_node
      "skip_to_tts"    → tts_node
      "skip_to_concat" → concat_node
      "fail"           → failed_node
    兜底：unknown action → rewrite_node
    """
    if state.get("current_step") == "failed":
        return "failed_node"
    action = state.get("next_action")
    return {
        "rewrite": "rewrite_node",
        "skip_to_tts": "tts_node",
        "skip_to_concat": "concat_node",
        "fail": "failed_node",
        "done": "__end__",  # CP-AGENT-ROUTE-DONE：全完成直接结束
    }.get(action, "rewrite_node")


def _route_after_rewrite(state: AgentState) -> str:
    """CP-AGENT-ROUTE-REWRITE：rewrite 后走 decision_router（Phase 3 复用同一个 router）。"""
    if state.get("current_step") == "failed":
        return "failed_node"
    return "decision_router_node"


def _route_after_tts(state: AgentState) -> str:
    """CP-AGENT-ROUTE-TTS：tts 后走 decision_router（让 LLM 决定要不要 concat 或跳过）。"""
    if state.get("current_step") == "failed":
        return "failed_node"
    return "decision_router_node"


# ---------------------------------------------------------------------------
# runner API
# ---------------------------------------------------------------------------


def build_agent_graph() -> Any:
    """CP-AGENT-RUNNER-BUILD：构造 LangGraph StateGraph（Phase 3 版）。

    Phase 3 拓扑：
      fetch_node → decision_router_node → [rewrite | tts | concat | failed]
      rewrite_node → decision_router_node → ...（复用同一个 router）
      tts_node → decision_router_node → ...
      concat_node → END
      failed_node → END

    Phase 1 旧版本（4 节点硬编码）：fetch → rewrite → tts → concat
    区别：每个非终点节点后多一个 decision_router_node 让 LLM 决策跳哪条边，
    简化 / 跳过 / 重做的能力。
    """
    g = StateGraph(AgentState)

    # 节点
    g.add_node("fetch_node", fetch_node)
    g.add_node("decision_router_node", decision_router_node)
    g.add_node("rewrite_node", rewrite_node)
    g.add_node("tts_node", tts_node)
    g.add_node("concat_node", concat_node)
    g.add_node("failed_node", failed_node)

    # 边
    g.add_edge(START, "fetch_node")
    g.add_conditional_edges(
        "fetch_node",
        _route_after_fetch,
        {
            "decision_router_node": "decision_router_node",
            "failed_node": "failed_node",
        },
    )
    g.add_conditional_edges(
        "decision_router_node",
        _route_after_decision_router,
        {
            "rewrite_node": "rewrite_node",
            "tts_node": "tts_node",
            "concat_node": "concat_node",
            "failed_node": "failed_node",
            "__end__": END,
        },
    )
    g.add_conditional_edges(
        "rewrite_node",
        _route_after_rewrite,
        {
            "decision_router_node": "decision_router_node",
            "failed_node": "failed_node",
        },
    )
    g.add_conditional_edges(
        "tts_node",
        _route_after_tts,
        {
            "decision_router_node": "decision_router_node",
            "failed_node": "failed_node",
        },
    )
    g.add_edge("concat_node", END)
    g.add_edge("failed_node", END)

    return g.compile()


# CP-AGENT-RECURSION-GUARD：LangGraph 默认 recursion_limit=10007，一旦决策路由器
# 反复返回同一个 next_action（LLM 抽风 / mock 配错），图会空转到超限再抛
# GraphRecursionError。这里设一个业务上限，并把超限翻译成 failed state。
#
# 正常路径最多 4 步 × 2 节点 ≈ 8 跳，25 足够宽松且能快速暴露死循环。
AGENT_RECURSION_LIMIT = 25


class _AgentApp:
    """CP-AGENT-APP-WRAPPER：包一层 compiled graph，统一 ainvoke 的配置与异常。

    - 注入 recursion_limit（防死循环）
    - GraphRecursionError → 返回 failed state（不把异常抛给 arq retry）
    """

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._graph.ainvoke(
                state, config={"recursion_limit": AGENT_RECURSION_LIMIT}
            )
        except GraphRecursionError as exc:
            log.error(
                f"agent_graph_recursion_limit article_id={state.get('article_id')} "
                f"limit={AGENT_RECURSION_LIMIT} error={exc!s}"
            )
            return {
                **state,
                "status": "failed",
                "current_step": "failed",
                "error": f"决策路由器反复返回同一动作，超过 {AGENT_RECURSION_LIMIT} 跳上限",
                "error_kind": "internal",
                "error_step": state.get("current_step", "decision"),
                "finished_at": _now(),
            }

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)


# 编译一次（import 时就能拿到 app）
agent_app = _AgentApp(build_agent_graph())
