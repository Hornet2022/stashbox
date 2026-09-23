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
from .openai import OpenAIClient
from .qwen import QwenVLClient

log = get_logger(__name__)

# CP7.3 实际实现了的 provider（deepseek / glm 在 base.py 里只是预留）
SUPPORTED_PROVIDERS = ("openai", "qwen_vl")

_client: LLMClient | None = None
_signature: str | None = None


def _env_config() -> dict[str, Any]:
    """第二层：环境变量；第三层：代码默认值。

    CP9.x 决策：LLM 全 OpenAI 协议栈。OPENAI_LLM_* 与 OPENAI_TTS_* 拆分避免
    双消费方冲突（详见 .env.example 注释）。
    """
    return {
        "provider": os.getenv("LLM_PROVIDER", "openai").lower(),
        # OpenAI provider 用的 env（与 TTS 端拆开）
        "openai_llm_api_key": os.getenv("OPENAI_LLM_API_KEY", ""),
        "openai_llm_model": os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini"),
        "openai_llm_base_url": os.getenv("OPENAI_LLM_BASE_URL", "https://api.openai.com/v1"),
        # CP7.1：qwen_vl（Token Plan 团队版，OpenAI 兼容）
        "qwen_vl_model": os.getenv("QWEN_VL_MODEL", "qwen3.6-flash"),
        "qwen_vl_base_url": os.getenv(
            "QWEN_VL_BASE_URL",
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
        "qwen_vl_api_key": os.getenv("DASHSCOPE_API_KEY", ""),
    }


def resolve_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """DB > env > 默认值。override 里 None/空串视为「没配」，回落到下一层。

    CP11.x 修复：管理后台 PUT /admin/llm/config 存的是**通用字段**
    （provider / model / api_key / base_url），而 build_client 读的是**分 provider
    字段**（openai_llm_* / qwen_vl_*）。此前两者没接上 —— 后台配的 key/model
    实际不生效（只有 provider 生效）。这里统一做一次映射归一化。
    """
    config = _env_config()
    for field, value in (override or {}).items():
        if value not in (None, ""):
            config[field] = value

    provider = str(config.get("provider") or "mock").lower()
    generic_key = config.get("api_key")
    generic_model = config.get("model")
    generic_base = config.get("base_url")
    if provider == "openai":
        if generic_key:
            config["openai_llm_api_key"] = generic_key
        if generic_model:
            config["openai_llm_model"] = generic_model
        if generic_base:
            config["openai_llm_base_url"] = generic_base
    elif provider == "qwen_vl":
        if generic_key:
            config["qwen_vl_api_key"] = generic_key
        if generic_model:
            config["qwen_vl_model"] = generic_model
        if generic_base:
            config["qwen_vl_base_url"] = generic_base
    return config


def build_client(config: dict[str, Any]) -> LLMClient:
    provider = str(config.get("provider") or "mock").lower()
    if provider == "openai":
        return OpenAIClient(
            api_key=config.get("openai_llm_api_key") or "",
            model=config.get("openai_llm_model") or "gpt-4o-mini",
            base_url=config.get("openai_llm_base_url") or "https://api.openai.com/v1",
        )
    if provider == "qwen_vl":
        return QwenVLClient(
            api_key=config.get("qwen_vl_api_key") or "",
            model=config.get("qwen_vl_model") or "qwen3.6-flash",
            base_url=config.get("qwen_vl_base_url")
            or "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
    raise ValueError(f"unsupported llm provider: {provider!r} (supported: openai/qwen_vl)")


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
            model=config.get("model")
            or config.get("openai_llm_model")
            or config.get("qwen_vl_model"),
            api_key_set=bool(
                config.get("api_key")
                or config.get("openai_llm_api_key")
                or config.get("qwen_vl_api_key")
            ),
        )
    return _client


async def current_config() -> dict[str, Any]:
    """当前生效的完整配置（DB > env），不建 client。"""
    return resolve_config(await get_config(KEY_LLM))


def _signature_of(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)
