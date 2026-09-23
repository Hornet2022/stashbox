"""LLM Client 工厂（基于 llm_settings.provider，CP3.5-pre-1）。

CP1.8+ 接 Nacos 动态配置时，provider 的来源从 LLMSettings 换成 Nacos，本文件对外接口不变。
注意：openai / qwen_vl 实现持有 httpx AsyncClient，用完要 `await client.close()`（或 async with）。

CP11.x：管理后台统一配置入口
- `await reload()` 读 system_config KEY_LLM（DB，Redis 5s 缓存）→ DB > env。
- 后台 PUT /admin/llm/config 存的是通用字段 provider / model / api_key / base_url，
  这里映射回分 provider 字段（openai_llm_* / qwen_vl_*）。
- `get_llm_client()` 同步签名不变：有 DB 覆盖用覆盖，否则回落 llm_settings（.env）。

CP3.6.2：factory 单例化
- `_create_cached_client()` 加 `@lru_cache(maxsize=8)` —— 同 (provider, model, api_key, base_url, timeout, max_retries) 返回同一实例。
- 单例 client 实例化时设 `_shared=True` → `close()` 变 no-op，避免破坏其他任务的 httpx 连接池。
- 关闭语义改为 `close_all_llm_clients()` 在 FastAPI lifespan shutdown 统一调用。
- 业务调用方（distill_task.py）改用 `maybe_close_llm_client(llm)` —— 单例 no-op，非单例正常关。
"""

from functools import lru_cache
import weakref
from typing import TYPE_CHECKING

from config_llm import llm_settings

from .base import LLMClient
from .openai import OpenAIClient
from .qwen_vl import QwenVLClient

if TYPE_CHECKING:
    pass

# DB 覆盖配置（reload() 写入；None = 还没读/读失败 → 走 env）
_db_config: dict | None = None


def _env_view() -> dict:
    """llm_settings 的等价 dict 视图（provider 路由用）。"""
    return {
        "provider": llm_settings.llm_provider,
        "model": None,
        "api_key": None,
        "base_url": None,
        "openai_llm_api_key": llm_settings.openai_llm_api_key,
        "openai_llm_model": llm_settings.openai_llm_model,
        "openai_llm_base_url": llm_settings.openai_llm_base_url,
        "qwen_vl_api_key": llm_settings.qwen_vl_api_key,
        "qwen_vl_model": llm_settings.qwen_vl_model,
        "qwen_vl_base_url": llm_settings.qwen_vl_base_url,
        "timeout": llm_settings.timeout,
        "max_retries": llm_settings.max_retries,
    }


async def reload() -> dict:
    """读 DB 配置（KEY_LLM），合并成生效配置并缓存；返回生效配置。

    蒸馏任务启动前调用（与 tts_reload 对称）→ 管理后台改 LLM 配置热生效。
    DB 里 provider 之外的字段（model / api_key / base_url）是「通用字段」，
    按 provider 映射到 openai_llm_* / qwen_vl_*。DB 无值时逐字段回落 env。
    """
    global _db_config
    cfg = _env_view()
    try:
        from stashbox.backend.common.system_config import KEY_LLM, get_config

        stored = await get_config(KEY_LLM) or {}
    except Exception:
        stored = {}

    if stored.get("provider"):
        cfg["provider"] = str(stored["provider"]).lower()
    provider = cfg["provider"]
    if stored.get("model"):
        cfg["model"] = str(stored["model"])
    if stored.get("api_key"):
        cfg["api_key"] = str(stored["api_key"])
    if stored.get("base_url"):
        cfg["base_url"] = str(stored["base_url"])

    # 通用字段 → 分 provider 字段（对齐 app/services/llm.resolve_config 的映射）
    if provider == "openai":
        if cfg.get("api_key"):
            cfg["openai_llm_api_key"] = cfg["api_key"]
        if cfg.get("model"):
            cfg["openai_llm_model"] = cfg["model"]
        if cfg.get("base_url"):
            cfg["openai_llm_base_url"] = cfg["base_url"]
    elif provider.startswith("qwen"):
        if cfg.get("api_key"):
            cfg["qwen_vl_api_key"] = cfg["api_key"]
        if cfg.get("model"):
            cfg["qwen_vl_model"] = cfg["model"]
        if cfg.get("base_url"):
            cfg["qwen_vl_base_url"] = cfg["base_url"]

    _db_config = cfg
    return cfg


def _effective() -> dict:
    return _db_config if _db_config is not None else _env_view()


# CP3.6.2：进程级单例池（maxsize=8 个不同配置）
# lru_cache 是强引用；用 weakref 跟踪所有 client 用于 lifespan shutdown 显式 close。
_tracked_clients: "weakref.WeakSet[LLMClient]" = weakref.WeakSet()


@lru_cache(maxsize=8)
def _create_cached_client(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    max_retries: int,
) -> LLMClient:
    """按 6 维参数缓存 client。同 (provider, model, ...) 返回同一实例。

    设 `_shared=True` → close() 变 no-op，避免破坏其他任务的 httpx 连接池。
    """
    if provider == "openai":
        client = OpenAIClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            _shared=True,
        )
    elif provider.startswith("qwen"):
        client = QwenVLClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            _shared=True,
        )
    else:
        raise ValueError(f"unsupported llm provider: {provider!r} (supported: openai/qwen_vl)")
    _tracked_clients.add(client)
    return client


def get_llm_client(model_name: str | None = None) -> LLMClient:
    """按生效配置（DB 覆盖 > .env）返回 cached client（+ model_name 兜底路由）。

    - "openai" → OpenAIClient（OpenAI 兼容协议，Bearer auth）
    - "qwen_vl" → QwenVLClient（阿里 token-plan 团队版 OpenAI 兼容）

    显式传 model_name（如 "qwen-vl-max" / "gpt-4o-mini"）时按模型名路由，
    方便按 Step 选模型（v1 §5.2.2 / §5.2.3）。

    CP3.6.2：返回 cached client —— 同 (provider, model, api_key, base_url, ...) 同实例。
    """
    cfg = _effective()
    provider = str(cfg.get("provider") or "openai").lower()

    # 路由：model_name 含 "gpt" → openai；含 "qwen" → qwen_vl；否则按 provider
    model_lower = (model_name or "").lower()
    if model_lower and "gpt" in model_lower:
        return _create_cached_client(
            "openai",
            model_name,
            cfg["openai_llm_api_key"],
            cfg["openai_llm_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
        )
    elif model_lower and "qwen" in model_lower:
        return _create_cached_client(
            "qwen_vl",
            model_name,
            cfg["qwen_vl_api_key"],
            cfg["qwen_vl_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
        )
    elif provider == "openai":
        return _create_cached_client(
            "openai",
            cfg["openai_llm_model"],
            cfg["openai_llm_api_key"],
            cfg["openai_llm_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
        )
    elif provider.startswith("qwen"):
        return _create_cached_client(
            "qwen_vl",
            cfg["qwen_vl_model"],
            cfg["qwen_vl_api_key"],
            cfg["qwen_vl_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
        )
    else:
        raise ValueError(f"unsupported llm provider: {provider!r} (supported: openai/qwen_vl)")


async def maybe_close_llm_client(client: LLMClient) -> None:
    """业务调用方（distill_task 等）关闭 client 的安全 helper。

    单例 client（`_shared=True`）→ no-op（httpx 池由 `close_all_llm_clients()` 在 lifespan shutdown 统一关闭）
    非单例 client → 正常 close。
    """
    if not getattr(client, "_shared", False):
        await client.close()


async def close_all_llm_clients() -> None:
    """FastAPI lifespan shutdown 关闭所有 cached clients。

    调用顺序：先遍历 tracked 调 close → 再 cache_clear 释放 lru_cache 引用。
    """
    for client in list(_tracked_clients):
        try:
            await client.close()
        except Exception:
            pass
    _create_cached_client.cache_clear()
    _tracked_clients.clear()


async def get_openai_client() -> OpenAIClient:
    """便捷 helper：直接拿 OpenAI 客户端（用于蒸馏任意 Step）。"""
    cfg = _effective()
    return _create_cached_client(  # type: ignore[return-value]
        "openai",
        cfg["model"] or cfg["openai_llm_model"],
        cfg["openai_llm_api_key"],
        cfg["openai_llm_base_url"],
        cfg["timeout"],
        cfg["max_retries"],
    )


async def get_qwen_vl_client() -> QwenVLClient:
    """便捷 helper：直接拿 Qwen VL 客户端（用于蒸馏 Step 1）。"""
    cfg = _effective()
    return _create_cached_client(  # type: ignore[return-value]
        "qwen_vl",
        cfg["model"] or cfg["qwen_vl_model"],
        cfg["qwen_vl_api_key"],
        cfg["qwen_vl_base_url"],
        cfg["timeout"],
        cfg["max_retries"],
    )
