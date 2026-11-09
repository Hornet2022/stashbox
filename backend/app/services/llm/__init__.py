"""LLM client 工厂 + 切换（CP7.1）。

⚠️ **配置源只有一个：DB**（`system_config` 表 key="llm"）。
   读取与规范化统一走 `stashbox.backend.common.upstream_config`，
   本模块**不再**读 env（2026-11-10 收敛）。

2026-11-10 为什么收敛（这是 2026-11-09 事故的根因所在）：
- 以前这里是「DB 覆盖 > env 逐字段回落」：DB 读失败（Redis/PG 抖动）就
  **静默**回落到 env —— 而 env 里残留着**已下线的端点**（旧 provider 的
  base_url / key）⇒ 请求打到死端点，**日志里看不出"配置没读到"**。
- 现在：DB 读失败 → 进程内 last-good；本进程从未成功读过 → 抛
  `UpstreamConfigUnavailable`（明确可操作），**不静默降级**。
- DB 缺字段 → 用**代码默认值**（不是 env）。

调用点：
- 同步 `get_llm_client()`：读**进程内缓存**（reload() 写）；没 reload 过 ⇒ 抛明确异常。
- `await reload()`：热生效入口，读 DB 配置后返回 client（配置变了才重建）。
- `await current_config()`：当前生效的完整配置（不建 client）。
"""

import json
from typing import Any

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.upstream_config import (
    LLM_CODE_DEFAULTS,
    current_llm,
    load_llm,
    resolve_llm,
)

from .base import LLMClient
from .openai import OpenAIClient
from .qwen import QwenVLClient

log = get_logger(__name__)

# CP7.3 实际实现了的 provider（deepseek / glm 在 base.py 里只是预留）
SUPPORTED_PROVIDERS = ("openai", "qwen_vl")

_client: LLMClient | None = None
_signature: str | None = None


def resolve_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """DB 里 llm 那一行 → 生效配置。

    ⚠️ **只认 DB**（不再有 env 层）：
    override 里 None/空串视为「没配」，落回**代码默认值**（不是 env）。
    override 是 **DB 行**（admin 端点传入），或 admin 测试端点传入的
    「表单值 + 已存值」合并结果 —— 两者都是"想生效的那份配置"。

    CP11.x 修复（保留）：管理后台 PUT /admin/llm/config 存的是**通用字段**
    （provider / model / api_key / base_url），而 build_client 读的是**分 provider
    字段**（openai_llm_* / qwen_vl_*）。映射统一由 upstream_config.resolve_llm
    实现 —— 全仓只有一份，不会漂移。
    """
    return resolve_llm(override)


def build_client(config: dict[str, Any]) -> LLMClient:
    provider = str(config.get("provider") or "mock").lower()
    if provider == "openai":
        return OpenAIClient(
            api_key=config.get("openai_llm_api_key") or "",
            model=config.get("openai_llm_model") or LLM_CODE_DEFAULTS["model"],
            base_url=config.get("openai_llm_base_url") or LLM_CODE_DEFAULTS["base_url"],
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
    """根据生效配置返回对应 client（同步，保持 CP7.1 的调用点不变）。

    ⚠️ 同步路径读**进程内缓存**（`reload()` 写的）。缓存空 ⇒ 抛
    `UpstreamConfigUnavailable` —— **不回落 env**。
    """
    global _client, _signature
    if _client is None:
        config = current_llm()
        _client = build_client(config)
        _signature = _signature_of(config)
    return _client


def build_adhoc_client(config: dict[str, Any]) -> LLMClient:
    """按**传入的** config 造一个一次性 client —— 不读 DB、不回落 env。

    存在的理由：管理后台的「测试调用」必须验证**运营刚填进表单**的那份配置。
    走 `reload()` 拿生效配置去测，会让填错的 base_url / model / api_key 也测出
    绿灯（测的是上一份已保存的配置），保存后才在生产推理时炸 —— 一次静默失败。

    与模块级单例的差别（这几条是刻意的）：
      - **不碰 `_client` 全局**：测试不该影响正在服务推理的那个 client；
      - 调用方负责 `await client.close()` —— 它不是共享实例。

    `config` 的键名与 `build_client` 完全一致（provider 前缀式：
    `openai_llm_api_key` / `qwen_vl_model` …），由调用方从请求体做映射。
    """
    return build_client(config)


async def reload() -> LLMClient:
    """重读配置（Redis 5s 缓存挡 DB 压力），配置变了才重建 client。

    DB 读失败且本进程没有 last-good ⇒ 抛 `UpstreamConfigUnavailable`。
    """
    global _client, _signature
    config, source = await load_llm()
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
            source=source,
        )
    return _client


async def current_config() -> dict[str, Any]:
    """当前生效的完整配置（**只认 DB**），不建 client。"""
    config, _source = await load_llm()
    return config


def _signature_of(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)
