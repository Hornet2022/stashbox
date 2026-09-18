"""LLM client 工厂 + 切换。"""
import os
from .base import LLMClient
from .mock import MockLLMClient
from .openai import OpenAIClient


def get_llm_client() -> LLMClient:
    """根据 settings.llm_provider 返回对应 client。

    Returns:
        LLMClient 实例（MockLLMClient / OpenAIClient / 等等）
    """
    provider = os.getenv("LLM_PROVIDER", "mock").lower()

    if provider == "mock":
        return MockLLMClient()
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        return OpenAIClient(api_key=api_key, model=model)
    else:
        # 默认 mock（fail-safe）
        return MockLLMClient()
