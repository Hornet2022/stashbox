"""听匣 agent 编排层（CP-AGENT-REWORK + CP-AGENT-MEMORY + CP-AGENT-MCP）。

替代 ai-service/distill/pipeline.py 的硬编码 4 步：
  Phase 1  ─ LangGraph StateGraph：fetch → rewrite → tts → concat
  Phase 2  ─ Tool registry（fetch_url / tts_synthesize / stage_cache_lookup / save_memory）
             + UserProfile memory + Few-shot memory store（PG 表读入到 prompt）
  Phase 3  ─ Agent loop：让 LLM 决策下一步 / 是否跳过 / 是否换 tool
             （TODO：下一轮做）
  Phase 4  ─ MCP 客户端接入外部 MCP server
             （TODO：下一轮做）

向后兼容：保持 DistillPipeline 的外部签名（ctx + run()），让 distill_task.py 不动
也能切到 LangGraph；老的 hooks / stage_cache / state_machine 也保留。
"""
