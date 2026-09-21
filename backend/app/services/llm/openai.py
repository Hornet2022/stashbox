"""OpenAI LLM 客户端（OpenAI 兼容 /chat/completions 端点）。

CP7.3.4：真实现。兼容一切 OpenAI 格式端点：
- 官方 api.openai.com/v1
- 阿里云百炼 compatible-mode/v1（qwen3.6-flash 等）
- 其他 OpenAI 兼容网关（vLLM / OneAPI 等）

请求体格式（OpenAI 兼容）::

    {
        "model": "qwen3.6-flash",
        "messages": [{"role": "user", "content": "..."}],
        "temperature": 0.7,
        "max_tokens": 2000,
    }

实现方式与 QwenVLClient 同构：httpx.AsyncClient 直发，不引 openai SDK
（少一个依赖；Token Plan 团队版端点本来就是 OpenAI 兼容格式）。
"""

import asyncio
from typing import Any

import httpx

from .base import LLMClient

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIClient(LLMClient):
    """OpenAI（兼容）客户端：chat() 调 {base_url}/chat/completions。"""

    def __init__(
        self,
        api_key: str,
        model: str = OPENAI_DEFAULT_MODEL,
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for OpenAIClient")
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def provider_name(self) -> str:
        return "openai"

    async def chat(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        return await self._post(body)

    async def _post(self, body: dict[str, Any]) -> str:
        """POST + 指数退避 retry（429 直接抛 RuntimeError，不重试）。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TimeoutException as e:
                last_err = e
            else:
                if resp.status_code == 429:
                    raise RuntimeError(
                        f"openai rate limit (429), retry_after={resp.headers.get('retry-after')}"
                    )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    last_err = e
                else:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)  # 指数退避

        raise RuntimeError(f"openai chat failed after {self.max_retries} attempts: {last_err}")

    async def close(self) -> None:
        await self._client.aclose()
