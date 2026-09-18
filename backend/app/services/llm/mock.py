"""Mock LLM 客户端。返回固定模板的"听感版本"改写文本。"""
import asyncio
import hashlib
from .base import LLMClient


_MOCK_TEMPLATES = [
    "这是一篇关于{topic}的文章。原文章的核心观点是：{key_point}。{summary}让我们用更自然的方式表达：{summary_natural}",
    "接下来你将听到这篇文章的核心内容。{summary}主要讲了{key_point}。让我们开始吧。",
    "本文摘自{topic}领域。作者的核心论点是：{key_point}。接下来我将为你详细解读。{summary}",
]


class MockLLMClient(LLMClient):
    """Mock LLM 客户端。

    不调任何外部 API，根据输入 prompt 的 hash 返回固定模板改写文本。
    用于本地开发 + CI 测试。
    """

    @property
    def provider_name(self) -> str:
        return "mock"

    async def chat(
        self,
        prompt: str,
        model: str = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        # 模拟 API 延迟
        await asyncio.sleep(0.5)

        # 用 prompt hash 选模板（同一 prompt 永远返回同一结果）
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        idx = int(prompt_hash, 16) % len(_MOCK_TEMPLATES)

        # 提取 prompt 里的关键词（简单实现：找 "原文：" 和 "URL：" 之间的内容）
        topic = self._extract_topic(prompt)
        key_point = self._extract_key_point(prompt)
        summary = self._extract_summary(prompt)

        template = _MOCK_TEMPLATES[idx]
        return template.format(
            topic=topic,
            key_point=key_point,
            summary=summary,
            summary_natural=summary,
        )

    def _extract_topic(self, prompt: str) -> str:
        if "URL：" in prompt:
            url = prompt.split("URL：")[1].split("\n")[0].strip()
            return url
        return "未知话题"

    def _extract_key_point(self, prompt: str) -> str:
        return "这篇文章提供了一个新的视角，让我们重新思考日常认知"

    def _extract_summary(self, prompt: str) -> str:
        return "作者通过具体案例分析了问题本质，并给出了可操作的建议。"
