"""Mock LLM Client（开发 + 单测，CP3.5-pre-1）。

按最后一条 user message 的关键词路由预置响应：
- "结构化" → Step 1（Qwen2.5-VL，v1 §5.2.2）风格的 JSON
- "改写"   → Step 2（Claude 4 Sonnet，v1 §5.2.3）风格的口语化听感稿
- 其他     → default 兜底
"""
import asyncio
import time
from collections.abc import AsyncIterator

from .base import LLMClient
from .types import ChatRequest, ChatResponse, Usage


# 预置响应模板（按 prompt 关键词路由）
_RESPONSES = {
    "结构化": '{"summary": "mock summary", "chapters": ["intro", "body"], "entities": ["AI"]}',
    "改写": "嘿！今天咱们聊聊 AI 行业最近的大动作...（mock 改写稿）",
}
_DEFAULT_RESPONSE = "Mock LLM response。Set llm_provider=claude/qwen_vl for real API。"


class MockLLMClient(LLMClient):
    def __init__(self, latency_ms: float = 100.0):
        self.latency_ms = latency_ms
        self._closed = False

    async def chat(self, req: ChatRequest) -> ChatResponse:
        try:
            resp = await self._mock_chat(req)
            await self._maybe_trace(req, resp=resp)
            return resp
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def _mock_chat(self, req: ChatRequest) -> ChatResponse:
        start = time.time()
        await asyncio.sleep(self.latency_ms / 1000.0)

        last_msg = req.messages[-1].content if req.messages else ""
        content = _DEFAULT_RESPONSE
        for keyword, response in _RESPONSES.items():
            if keyword in last_msg:
                content = response
                break

        # token 估算（mock 用 len/4，精确计数留 CP3.5 挂 tiktoken / 官方 SDK）
        prompt_tokens = sum(len(m.content) for m in req.messages) // 4
        completion_tokens = len(content) // 4

        return ChatResponse(
            content=content,
            model=req.model or "mock-model",
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            latency_ms=(time.time() - start) * 1000,
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """mock 流式：按句号切块，每 50ms 吐一个 chunk。

        usage 已经在内部 chat() 里上报过，这里只在失败时标 ERROR，不重复计数。
        """
        try:
            chunks = (await self.chat(req)).content.split("。")
            for chunk in chunks:
                yield chunk + "。"
                await asyncio.sleep(0.05)
        except Exception as e:
            await self._maybe_trace(req, error=e)
            raise

    async def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def close(self) -> None:
        self._closed = True
