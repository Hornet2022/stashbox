"""ai-service 私有 LLM Client 抽象层（CP3.5-pre-1，v1 §5 L4 蒸馏引擎）。

用法::

    from llm import get_llm_client

    client = get_llm_client()               # 默认随配置路由（openai / qwen_vl）
    resp = await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="...")]))

ai-service 目录名带连字符（不是合法包名），不能相对导入上层 config_llm.py ——
这里把 ai-service 目录加进 sys.path，让 config_llm 作为顶层模块导入。

⚠️ 注意：与 content-service 用的 app/services/llm/（CP7.1）是**两个独立的 LLM 抽象层**
（接口不同构：这里 chat(ChatRequest)->ChatResponse，那边 chat(prompt)->str），
但 CP11.x 起**配置源已统一**：两层都读 system_config KEY_LLM（管理后台
/admin/llm/config 一个入口），env 兜底。蒸馏任务启动前 factory.reload() 刷新。
"""

import sys
from pathlib import Path

_AI_SERVICE_DIR = str(Path(__file__).resolve().parent.parent)
if _AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, _AI_SERVICE_DIR)

from .base import LLMClient  # noqa: E402
from .exceptions import LLMError, RateLimitError, TokenLimitError  # noqa: E402
from .factory import (  # noqa: E402
    close_all_llm_clients,
    get_llm_client,
    get_openai_client,
    get_qwen_vl_client,
    maybe_close_llm_client,
    reload,
)
from .openai import OpenAIClient  # noqa: E402
from .qwen_vl import QwenVLClient  # noqa: E402
from .types import (  # noqa: E402
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ToolCall,
    Usage,
)

__all__ = [
    "LLMClient",
    "OpenAIClient",
    "QwenVLClient",
    "get_llm_client",
    "get_openai_client",
    "get_qwen_vl_client",
    "maybe_close_llm_client",  # CP3.6.2: 单例 client 安全关闭 helper
    "close_all_llm_clients",  # CP3.6.2: lifespan shutdown 关闭所有 cached clients
    "reload",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ToolCall",
    "Usage",
    "LLMError",
    "RateLimitError",
    "TokenLimitError",
]
