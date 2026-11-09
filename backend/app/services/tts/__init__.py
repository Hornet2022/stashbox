"""TTS client 工厂 + 热生效（CP TTS-Config）。

⚠️ **配置源只有一个：DB**（`system_config` 表 key="tts"）。
   读取与规范化统一走 `stashbox.backend.common.upstream_config`，
   本模块**不再**读 env（2026-11-10 收敛）。

2026-11-10 为什么收敛：
- 以前这里是「DB 覆盖 > env 逐字段回落」，而 env 里残留着**已下线的端点**。
  DB 读失败（Redis/PG 抖动）就**静默**回落 env ⇒ 音色从 uncle_fu 悄悄换成
  zh-CN-XiaoxiaoNeural，**已发布的稿子突然变声**，而且没有任何报错。
- 现在：DB 读失败 → 进程内 last-good；本进程从未成功读过 → 抛
  `UpstreamConfigUnavailable`（明确可操作），**不静默降级**。
- DB 缺字段 → 用**代码默认值**（不是 env）。

调用点：
- 同步 `get_tts_client()`：读**进程内缓存**（reload() 写）；没 reload 过 ⇒ 抛明确异常。
- `await reload()`：热生效入口，读 DB 配置后返回 client（配置变了才重建）。
- `await current_config()`：当前生效的完整配置（不建 client）。
"""

import json
from typing import Any

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.upstream_config import (
    current_tts,
    load_tts,
    resolve_tts,
)

from .base import TTSClient
from .edge import EdgeTTSClient
from .openai import OpenAITTSClient
from .doubao import DoubaoTTSClient
from .local import LocalTTSClient
from .indextts import IndexTTSClient

log = get_logger(__name__)

SUPPORTED_PROVIDERS = ("edge", "openai", "doubao", "local", "indextts")

_client: TTSClient | None = None
_signature: str | None = None


def resolve_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """DB 里 tts 那一行 → 生效配置。

    ⚠️ **只认 DB**（不再有 env 层）：
    override 里 None/空串视为「没配」，落回**代码默认值**（不是 env）。

    TTS 的 DB 行**本来就是 provider 专属命名**（openai_api_key / indextts_model …），
    所以这里不需要任何"通用 ↔ 专属"映射 —— 与 LLM 侧不同，别照抄那边的逻辑。
    """
    return resolve_tts(override)


def build_client(config: dict[str, Any]) -> TTSClient:
    provider = str(config.get("provider") or "mock").lower()
    if provider == "edge":
        return EdgeTTSClient(voice=config.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
    if provider == "openai":
        return OpenAITTSClient(
            api_key=config.get("openai_api_key") or "",
            base_url=config.get("openai_base_url") or "https://api.openai.com/v1",
            model=config.get("openai_model") or "tts-1",
            voice=config.get("openai_voice") or "alloy",
            instructions=config.get("openai_instructions") or "",
            language=config.get("openai_language") or "",
        )
    if provider == "doubao":
        return DoubaoTTSClient(
            app_id=config.get("doubao_app_id") or "",
            token=config.get("doubao_token") or "",
            api_key=config.get("doubao_api_key") or "",
            voice=config.get("doubao_voice") or "BV001_streaming",
            resource_id=config.get("doubao_resource_id") or "seed-tts-2.0",
        )
    if provider == "local":
        return LocalTTSClient(
            voice=config.get("local_voice") or "Tingting",
            ffmpeg_bin=config.get("ffmpeg_bin") or "/opt/homebrew/bin/ffmpeg",
        )
    if provider == "indextts":
        return IndexTTSClient(
            base_url=config.get("indextts_base_url") or "http://127.0.0.1:8010/v1",
            model=config.get("indextts_model") or "Qwen3-TTS-12Hz-0.6B-Base-bf16",
            ref_audio_path=config.get("indextts_ref_audio") or "",
            ref_text=config.get("indextts_ref_text") or "",
            speaker=config.get("indextts_speaker") or "",
            instruct=config.get("indextts_instruct") or "",
            temperature=config.get("indextts_temperature"),
            top_p=config.get("indextts_top_p"),
        )
    raise ValueError(
        f"unsupported tts provider: {provider!r} " f"(supported: edge/openai/doubao/local/indextts)"
    )


def get_tts_client() -> TTSClient:
    """根据生效配置返回对应 client（同步，保持旧调用点不变）。

    ⚠️ 同步路径读**进程内缓存**（`reload()` 写的）。缓存空 ⇒ 抛
    `UpstreamConfigUnavailable` —— **不回落 env**。

    为什么不能"没 reload 过就按默认值建一个"：
    那样 TTS 会**静默**用默认音色（zh-CN-XiaoxiaoNeural），
    而实际生效的是 uncle_fu ⇒ 已发布的稿子突然变声，且没有任何报错。
    """
    global _client, _signature
    if _client is None:
        config = current_tts()
        _client = build_client(config)
        _signature = _signature_of(config)
    return _client


async def reload() -> TTSClient:
    """重读配置（Redis 5s 缓存挡 DB 压力），配置变了才重建 client。

    PUT /api/v1/admin/tts/config 写完会立刻 await reload() → 下一次蒸馏
    直接用新 client，不需要重启进程。

    DB 读失败且本进程没有 last-good ⇒ 抛 `UpstreamConfigUnavailable`。
    """
    global _client, _signature
    config, source = await load_tts()
    signature = _signature_of(config)
    if _client is None or signature != _signature:
        _client = build_client(config)
        _signature = signature
        log.info(
            "tts_client_reloaded",
            provider=config.get("provider"),
            api_key_set=bool(config.get("openai_api_key") or config.get("doubao_api_key")),
            source=source,
        )
    return _client


async def current_config() -> dict[str, Any]:
    """当前生效的完整配置（**只认 DB**），不建 client。"""
    config, _source = await load_tts()
    return config


def _signature_of(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)
