"""LLM 通用数据类型（CP3.5-pre-1 —— v1 §5 L4 蒸馏引擎）。"""
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None  # for tool role
    tool_call_id: str | None = None  # for tool response


class ToolCall(BaseModel):
    """工具调用（CP3.5+ 才用，留接口）。"""

    id: str
    type: str = "function"
    function: dict  # {"name": ..., "arguments": "..."}


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None  # 不传则用 client 默认
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    stop: list[str] | None = None
    tools: list[dict] | None = None
    metadata: dict | None = None  # 用于 trace（langfuse / request_id）


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    content: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"  # "stop" / "length" / "tool_calls"
    tool_calls: list[ToolCall] | None = None
    latency_ms: float = 0.0
