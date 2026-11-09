"""LLM Client 工厂（CP3.5-pre-1）。

⚠️ **配置源只有一个：DB**（`system_config` 表 key="llm"）。
   读取与规范化统一走 `stashbox.backend.common.upstream_config`，
   本文件**不再**读 env —— 见该模块 docstring 里 2026-11-09/10 事故的完整复盘。

2026-11-10 收敛（单一真相源）：
- 以前这里是「DB 覆盖 > env 逐字段回落」：DB 读失败就**静默**回落到 env，
  而 env 里还残留着**已下线的端点** ⇒ 请求打到死端点，日志里看不出"配置没读到"。
- 现在：DB 读失败 → 用进程内 last-good；本进程从未成功读过 → 抛
  `UpstreamConfigUnavailable`（明确可操作），**不静默降级**。
- DB 缺字段 → 用**代码默认值**（不是 env）。
- 「通用字段 ↔ provider 专属字段」的映射只在 upstream_config 里实现一次。

调用方：
- `await reload()`：读 DB → 规范化 → 缓存（蒸馏任务启动前调用，热生效）。
- `get_llm_client()`：同步签名不变，读**进程内缓存**；缓存空 ⇒ 抛明确异常。

CP3.6.2：factory 单例化
- `_create_cached_client()` 加 `@lru_cache(maxsize=8)` —— 同 (provider, model, api_key, base_url, timeout, max_retries, connect_timeout) 返回同一实例。
- 单例 client 实例化时设 `_shared=True` → `close()` 变 no-op，避免破坏其他任务的 httpx 连接池。
- 关闭语义改为 `close_all_llm_clients()` 在 FastAPI lifespan shutdown 统一调用。
- 业务调用方（distill_task.py）改用 `maybe_close_llm_client(llm)` —— 单例 no-op，非单例正常关。
"""

from functools import lru_cache
import weakref

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.upstream_config import (
    LLM_CODE_DEFAULTS,
    current_llm,
    load_llm,
)

from .base import LLMClient
from .openai import OpenAIClient
from .qwen_vl import QwenVLClient

log = get_logger(__name__)

# 生效配置（reload() 写入；None = 本进程还没成功读过 DB ⇒ 抛明确异常，不回落 env）
_db_config: dict | None = None


async def reload() -> dict:
    """读 DB 配置（KEY_LLM）→ 规范化 → 缓存；返回生效配置。

    蒸馏任务启动前调用（与 tts_reload 对称）→ 管理后台改 LLM 配置热生效。

    读失败且本进程没有 last-good ⇒ 抛 `UpstreamConfigUnavailable`
    （**不回落 env**：那正是 2026-11-09 事故里"打到已下线端点"的根因）。
    """
    global _db_config
    cfg, source = await load_llm()
    _db_config = cfg
    log.info(
        "llm_config_loaded",
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        base_url=cfg.get("base_url"),
        api_key_set=bool(cfg.get("api_key")),
        source=source,
    )
    return cfg


def _effective() -> dict:
    """当前生效配置。

    优先用本模块缓存（reload() 写的）；没有则读 upstream_config 的进程内缓存
    （content-service 与 ai-service 共用同一份 last-good 语义）。
    两者都空 ⇒ 抛 `UpstreamConfigUnavailable` —— **不回落 env**。
    """
    if _db_config is not None:
        return _db_config
    return current_llm()


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
    connect_timeout: float = 10.0,
) -> LLMClient:
    """按 7 维参数缓存 client。同 (provider, model, ...) 返回同一实例。

    设 `_shared=True` → close() 变 no-op，避免破坏其他任务的 httpx 连接池。
    """
    # 生效配置**打一条日志**（2026-10-24 补）。
    #
    # 为什么必须有：配置来源是「DB 覆盖 > env」，而 **env 里可能是过期的值**
    # （实测生产 env 里 TTS_PROVIDER=edge、OPENAI_LLM_BASE_URL=api.openai.com
    # 都与 DB 不一致）。一旦 DB 读失败（Redis/PG 抖动）就会**静默回落**到 env：
    #   · LLM → 拿着空 key 打 api.openai.com（国内不可达）⇒ 症状是 401/连接失败，
    #     **完全看不出是「配置没读到」** ⇒ 排查方向被带偏；
    #   · TTS → 变成 edge-tts ⇒ 音色从 uncle_fu 换成 zh-CN-XiaoxiaoNeural，
    #     **已发布的稿子突然变声**，而且没有任何报错。
    #
    # 所以**每次真正建 client** 都把「指向哪个端点、用的哪个模型、key 有没有」
    # 记下来（**不记 key 本身**）。回落一眼可见。
    log.info(
        "llm_effective_config provider=%s base_url=%s model=%s api_key_set=%s source=%s",
        provider,
        base_url,
        model,
        bool(api_key),
        "db" if _db_config is not None else "env",
    )
    if provider == "openai":
        client = OpenAIClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            connect_timeout=connect_timeout,
            _shared=True,
        )
    elif provider.startswith("qwen"):
        client = QwenVLClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            connect_timeout=connect_timeout,
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
            cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
        )
    elif model_lower and "qwen" in model_lower:
        return _create_cached_client(
            "qwen_vl",
            model_name,
            cfg["qwen_vl_api_key"],
            cfg["qwen_vl_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
            cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
        )
    elif provider == "openai":
        return _create_cached_client(
            "openai",
            cfg["openai_llm_model"],
            cfg["openai_llm_api_key"],
            cfg["openai_llm_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
            cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
        )
    elif provider.startswith("qwen"):
        return _create_cached_client(
            "qwen_vl",
            cfg["qwen_vl_model"],
            cfg["qwen_vl_api_key"],
            cfg["qwen_vl_base_url"],
            cfg["timeout"],
            cfg["max_retries"],
            cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
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
        cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
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
        cfg.get("connect_timeout", LLM_CODE_DEFAULTS["connect_timeout"]),
    )
