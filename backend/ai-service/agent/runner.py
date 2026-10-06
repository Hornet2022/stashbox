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

from .state import AgentState
from .tools import ToolError, get_default_registry

# CP-AGENT-LLM-IMPORT：动态模块引用而不是 `from llm import get_llm_client`，
# 让测试 monkeypatch `llm.get_llm_client` 时真正生效（`from X import Y` 会绑死
# 模块级引用，monkeypatch 改不到）。
import llm as _llm_module
from llm.types import ChatMessage, ChatRequest

log = logging.getLogger("agent.runner")


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


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

        raw = await _chat_text(
            llm,
            prompt,
            system_prompt=_REWRITE_SYSTEM,
            step="agent_rewrite",
            task_id=state.get("trace_id", ""),
        )
        # 结构化解析（CP-AGENT-REWRITE-STRUCTURED）：LLM 必须回 JSON
        # {hook, sections, outro}，这样 script_text 的段落边界才是可靠的。
        parts = _parse_rewrite(raw)
        log.info(
            "agent_rewrite_parsed hook=%dB sections=%d outro=%dB fallback=%s",
            len(parts.hook),
            len(parts.sections),
            len(parts.outro),
            parts.degraded,
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
            "rewrite_tags": [],
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
  "sections": ["正文节拍，一节一个字符串，80-180 字，共 3-6 节"],
  "outro": "收束，≤ 80 字，呼应 hook 的开头句式或意象"
}

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
9. **长度自适应**：原文 < 500 字 → 600-800 字；500-2000 字 → 800-1100 字；> 2000 字 → 1000-1400 字

输出纯 JSON。"""


def _rewrite_prompt(fetched_content: str) -> str:
    """构造改写 prompt（user 侧）。

    system 契约放在 `_chat_text(system_prompt=...)`，这里只拼原文。
    """
    return f"# 原文\n{fetched_content}"


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
    """
    obj = _extract_json_object(raw)
    if obj is not None:
        hook = _flatten_para(str(obj.get("hook") or ""))
        raw_sections = obj.get("sections") or []
        if isinstance(raw_sections, str):
            raw_sections = [raw_sections]
        sections = [_flatten_para(str(s)) for s in raw_sections if str(s).strip()]
        outro = _flatten_para(str(obj.get("outro") or ""))
        # hook 缺失但有正文 → 拿第一节顶上，别让整稿没有钩子
        if not hook and sections:
            hook, sections = sections[0], sections[1:]
        if hook or sections or outro:
            return _RewriteParts(hook=hook, sections=sections, outro=outro)

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
    return getattr(resp, "content", None) or str(resp)


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
