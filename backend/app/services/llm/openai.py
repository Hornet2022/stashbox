"""OpenAI LLM 客户端（生产用，留接口待实现）。"""
from .base import LLMClient


class OpenAIClient(LLMClient):
    """OpenAI LLM 客户端（接口预留，未实现）。

    切换到真 API 时：
    - pip install openai
    - 实现 chat() 调 openai.AsyncClient
    - 从 settings.openai_api_key 读 key
    """

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model

    @property
    def provider_name(self) -> str:
        return "openai"

    async def chat(
        self,
        prompt: str,
        model: str = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        raise NotImplementedError(
            "OpenAIClient 待实现。pip install openai + 实现 chat()。"
            "Mock 方案下用 MockLLMClient。"
        )
