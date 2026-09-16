"""LLM Client 工厂（基于 llm_settings.provider，CP3.5-pre-1）。

CP1.8+ 接 Nacos 动态配置时，provider 的来源从 LLMSettings 换成 Nacos，本文件对外接口不变。
注意：claude / qwen_vl 实现持有 httpx AsyncClient，用完要 `await client.close()`（或 async with）。
"""
# ai-service 目录名带连字符（不是合法包名），llm/__init__.py 已把它加进 sys.path，
# config_llm 因此作为顶层模块导入（而不是相对导入上层的包）。
from config_llm import llm_settings

from .base import LLMClient
from .claude import ClaudeSonnetClient
from .mock import MockLLMClient
from .qwen_vl import QwenVLClient


def get_llm_client(model_name: str | None = None) -> LLMClient:
    """根据 llm_settings.provider（+ model_name 兜底路由）返回具体实现。

    - "mock"（默认）→ MockLLMClient（开发 + 单测）
    - "claude" → ClaudeSonnetClient（CP3.5 接真 API）
    - "qwen_vl" → QwenVLClient（CP3.5 接真 API）

    显式传 model_name（如 "claude-4-sonnet-20250514" / "qwen2.5-vl-72b-instruct"）
    时按模型名路由，即使 provider=mock —— 方便按 Step 选模型（v1 §5.2.2 / §5.2.3）。
    """
    provider = llm_settings.llm_provider

    if provider == "claude" or (model_name and "claude" in model_name.lower()):
        return ClaudeSonnetClient(
            api_key=llm_settings.claude_api_key,
            model=model_name or llm_settings.claude_model,
            timeout=llm_settings.timeout,
            max_retries=llm_settings.max_retries,
        )
    elif provider == "qwen_vl" or (model_name and "qwen" in model_name.lower()):
        return QwenVLClient(
            api_key=llm_settings.qwen_vl_api_key,
            model=model_name or llm_settings.qwen_vl_model,
            timeout=llm_settings.timeout,
            max_retries=llm_settings.max_retries,
        )
    else:
        return MockLLMClient(latency_ms=100.0)


async def get_claude_client() -> ClaudeSonnetClient:
    """便捷 helper：直接拿 Claude 客户端（用于蒸馏 Step 2 / Step 3）。"""
    return ClaudeSonnetClient(
        api_key=llm_settings.claude_api_key,
        model=llm_settings.claude_model,
        timeout=llm_settings.timeout,
        max_retries=llm_settings.max_retries,
    )


async def get_qwen_vl_client() -> QwenVLClient:
    """便捷 helper：直接拿 Qwen VL 客户端（用于蒸馏 Step 1）。"""
    return QwenVLClient(
        api_key=llm_settings.qwen_vl_api_key,
        model=llm_settings.qwen_vl_model,
        timeout=llm_settings.timeout,
        max_retries=llm_settings.max_retries,
    )
