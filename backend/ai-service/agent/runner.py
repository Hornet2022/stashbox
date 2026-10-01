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
import logging
import os
import time
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

        base_prompt = (
            f"请把以下原文改写成一篇适合通勤收听的「听感稿」，"
            f"约 800-1200 字，保留关键事实与数据。\n\n"
            f"# 原文\n{fetched_content}"
        )

        # Phase 2：memory 注入（如果有 store + profile）
        if user_profile or few_shot_examples:
            # Note: MemoryStore 实例化依赖 session_factory；这里用延迟注入模式
            # —— runner 调用方注入（避免 agent 模块依赖 DB 配置）。
            prompt = _maybe_inject_memory(state, base_prompt)
        else:
            prompt = base_prompt

        script = await _chat_text(
            llm,
            prompt,
            step="agent_rewrite",
            task_id=state.get("trace_id", ""),
        )
        # 真实实现：从 script 提取 quality_score + tags
        # Phase 1 简化：score=None，tags=[]
        return {
            "rewritten_script": script,
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
                "（音频已落本地磁盘；OSS 上传未实现，storage/oss.py 仍是空壳）。"
                "停止重复合成 —— 上传能力就绪前不会再重试。",
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
