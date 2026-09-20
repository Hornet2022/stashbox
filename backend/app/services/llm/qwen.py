"""Qwen VL Client（文本，用于 admin /admin/llm/test 端点真验）。

Token Plan 团队版（2026-09-20）—— OpenAI 兼容端点：
`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions`

请求体格式（OpenAI 兼容）::

    {
        "model": "qwen3.6-flash",
        "messages": [{"role": "user", "content": "..."}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
"""

import asyncio
from typing import Any

import httpx

from .base import LLMClient

QWEN_VL_DEFAULT_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
QWEN_VL_DEFAULT_MODEL = "qwen3.6-flash"


class QwenVLClient(LLMClient):
    """Qwen VL Client（OpenAI 兼容 API，admin 端点用）。

    用途：CP7.1 admin-web 配置页选 qwen_vl provider 时，真调用 Token Plan 团队版。
    base_url 指向 OpenAI 兼容端点（/chat/completions）。
    """

    def __init__(
        self,
        api_key: str,
        model: str = QWEN_VL_DEFAULT_MODEL,
        base_url: str = QWEN_VL_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required for QwenVLClient")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
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
        return "qwen_vl"

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
                        f"qwen_vl rate limit (429), retry_after={resp.headers.get('retry-after')}"
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

        raise RuntimeError(f"qwen_vl chat failed after {self.max_retries} attempts: {last_err}")

    async def close(self) -> None:
        await self._client.aclose()
