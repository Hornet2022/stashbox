"""CP-AGENT-STATE：听匣 agent 的状态 schema。

LangGraph StateGraph 在每节点之间共享 AgentState 实例。节点返回 dict，
LangGraph 自动 merge 到 State 上（reducer 默认覆盖）。

设计要点：
  - 必填字段：article_id / user_id / current_step / status
  - 上下文：fetched_content / rewritten_script / tts_audio_url / final_audio_url
  - 改写结构：rewrite_hook / rewrite_sections / rewrite_outro
    （CP-AGENT-REWRITE-STRUCTURED：LLM 回 JSON，代码拼整稿并保证段落边界）
  - 决策：next_action（让 Phase 3 的 LLM router 写，节点读取决定走哪条边）
  - 错误：error / error_step / error_kind（CP-LLM-TEST-ERR / CP-TTS-TEST-ERR 兼容）
  - 工具结果：tool_results（list[dict]）
  - 记忆：user_profile / few_shot_examples（注入 prompt 的记忆）
"""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # === 必填：每个 distill run 的入参 ===
    article_id: str
    user_id: int
    url: str
    source: str  # wechat / douyin / pdf / web / unknown

    # === 流程控制 ===
    current_step: str  # 'fetch' / 'rewrite' / 'tts' / 'concat' / 'done' / 'failed'
    status: str  # DistillStatus 字符串值（与 state_machine 对齐）
    next_action: Optional[str]  # Phase 3 用：LLM router 写入 "fetch|rewrite|tts|concat|skip|done"

    # === 上下文：节点产出 ===
    fetched_content: Optional[str]  # step1 fetch_url 拿到的原文 markdown
    fetched_meta: Optional[dict[str, Any]]  # 标题/作者/发布时间/字数
    rewritten_script: Optional[str]  # step2 LLM 改写后的听感稿（整稿，段落边界 = \n\n）
    # CP-AGENT-REWRITE-STRUCTURED：LLM 按 JSON 契约回 {hook, sections, outro}，
    # 解析后原样存这三项。`rewritten_script` 是我们按这三项拼的，段落边界
    # 由代码保证 —— 下游 few-shot 入池取 `split("\n\n",1)[0]` 拿到的才是 hook。
    rewrite_hook: Optional[str]
    rewrite_sections: Optional[list[str]]
    rewrite_outro: Optional[str]
    rewrite_quality_score: Optional[float]
    rewrite_tags: Optional[list[str]]
    # CP-AGENT-IS-PERSONALIZED：as-treated 口径 —— 这次改写**实际**有没有用上
    # 个性化记忆（few-shot 或用户画像）。之前这个字段不存在，导致
    # distill_task 的 `bool(final.get("is_personalized"))` 恒为 False。
    is_personalized: Optional[bool]
    tts_audio_url: Optional[str]  # step3 TTS 合成音频 OSS URL
    tts_audio_path: Optional[str]  # 音频本地落盘路径（OSS 未就绪时的真实产物）
    tts_duration_sec: Optional[int]
    # CP-TTS-VOICE：本次蒸馏实际用的音色（tts_voices.id）。
    # None = 回落全局 indextts_ref_audio 配置，来源不可溯源。
    # 回写 distilled_articles.tts_voice_id，让「这篇音频是谁念的」可追溯。
    tts_voice_id: Optional[str]
    tts_voice_name: Optional[str]
    final_audio_url: Optional[str]  # step4 拼接后最终音频 OSS URL
    final_duration_sec: Optional[int]

    # === 工具结果（Phase 2 tool registry 用） ===
    tool_calls: list[dict[str, Any]]  # [{name, args, result, ts}]
    tool_results: dict[str, Any]  # key=tool_name, value=last result（cache）

    # === 错误（与后端 _classify_llm_error / _classify_tts_error 兼容） ===
    error: Optional[str]
    error_step: Optional[str]
    error_kind: Optional[str]  # 'timeout' / 'auth' / 'network' / ...
    retry_after: Optional[str]  # 429 时用

    # === 记忆（Phase 2 user_profile + few-shot） ===
    user_profile: Optional[dict[str, Any]]  # {tier, preferences, cohort, ...}
    few_shot_examples: Optional[list[dict[str, Any]]]  # [{input, output, score}]

    # === 元信息 ===
    started_at: str  # ISO8601
    finished_at: Optional[str]  # ISO8601
    trace_id: str  # 用于 log 串联
