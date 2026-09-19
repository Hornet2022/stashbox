"""LLM client 工厂 + 切换（CP7.1）。

CP7.3：配置来源优先级 **DB(system_config key="llm") > 环境变量 > 代码默认值**。
DB 那层读走 Redis 5s 缓存（common.system_config），所以每次 reload() 都重读配置
也不会打爆 DB；只有配置签名真的变了才重建 client —— admin 改完配置，
下一次调用即生效，不用重启进程。

调用点：
- 同步 `get_llm_client()`：CP7.1 的旧调用点（ai-service 蒸馏任务）保持不变，
  没 reload 过时按 env/默认值建一个并缓存。
- `await reload()`：热生效入口，读 DB 配置后返回 client（配置变了才重建）。
"""

import json
import os
from typing import Any

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.system_config import KEY_LLM, get_config

from .base import LLMClient
from .mock import MockLLMClient
from .openai import OpenAIClient

log = get_logger(__name__)

# CP7.3 实际实现了的 provider（deepseek / glm 在 base.py 里只是预留）
SUPPORTED_PROVIDERS = ("mock", "openai")

_client: LLMClient | None = None
_signature: str | None = None


def _env_config() -> dict[str, Any]:
    """第二层：环境变量；第三层：代码默认值。"""
    return {
        "provider": os.getenv("LLM_PROVIDER", "mock").lower(),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        # CP7.3.3：base_url 只做「读出来给 admin 展示」这一层，client 怎么用它不在本任务范围
        "base_url": os.getenv("LLM_BASE_URL", ""),
    }


def resolve_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """DB > env > 默认值。override 里 None/空串视为「没配」，回落到下一层。"""
    config = _env_config()
    for field, value in (override or {}).items():
        if value not in (None, ""):
            config[field] = value
    return config


def build_client(config: dict[str, Any]) -> LLMClient:
    provider = str(config.get("provider") or "mock").lower()
    if provider == "openai":
        return OpenAIClient(
            api_key=config.get("api_key") or "",
            model=config.get("model") or "gpt-4o-mini",
        )
    if provider != "mock":
        log.warning("llm_provider_unsupported", provider=provider, fallback="mock")
    return MockLLMClient()


def get_llm_client() -> LLMClient:
    """根据配置返回对应 client（同步，保持 CP7.1 的调用点不变）。"""
    global _client, _signature
    if _client is None:
        config = resolve_config(None)
        _client = build_client(config)
        _signature = _signature_of(config)
    return _client


async def reload() -> LLMClient:
    """重读配置（Redis 5s 缓存挡 DB 压力），配置变了才重建 client。"""
    global _client, _signature
    config = resolve_config(await get_config(KEY_LLM))
    signature = _signature_of(config)
    if _client is None or signature != _signature:
        _client = build_client(config)
        _signature = signature
        log.info(
            "llm_client_reloaded",
            provider=config["provider"],
            model=config["model"],
            api_key_set=bool(config.get("api_key")),
        )
    return _client


async def current_config() -> dict[str, Any]:
    """当前生效的完整配置（DB > env），不建 client。"""
    return resolve_config(await get_config(KEY_LLM))


def _signature_of(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)
