"""LLM client 抽象接口。"""
from abc import ABC, abstractmethod
from typing import Optional


class LLMClient(ABC):
    """LLM 蒸馏客户端抽象。

    实现类：
    - MockLLMClient（开发/CI 用）
    - OpenAIClient（生产，留接口）
    - DeepSeekClient（生产，留接口）
    - GLMClient（生产，留接口）
    """

    @abstractmethod
    async def chat(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        """调用 LLM，返回生成文本。

        Args:
            prompt: 用户提示词（蒸馏 prompt）
            model: 模型名（None 用默认）
            max_tokens: 最大输出 token
            temperature: 温度

        Returns:
            LLM 生成的文本（蒸馏后的"听感版本"）

        Raises:
            Exception: 调用失败
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 名（mock/openai/deepseek/glm）。"""
        pass
