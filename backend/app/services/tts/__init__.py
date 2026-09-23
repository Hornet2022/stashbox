"""TTS client 工厂 + 热生效（CP TTS-Config）。

配置来源优先级 **DB(system_config key="tts") > 环境变量 > 代码默认值**。
DB 那层读走 Redis 5s 缓存（common.system_config），所以每次 reload() 都重读配置
也不会打爆 DB；只有配置签名真的变了才重建 client —— admin 改完配置，
下一次调用即生效，不用重启进程。

调用点：
- 同步 `get_tts_client()`：旧调用点（ai-service 蒸馏任务）保持不变，
  没 reload 过时按 env/默认值建一个并缓存。
- `await reload()`：热生效入口，读 DB 配置后返回 client（配置变了才重建）。
"""

import json
import os
from typing import Any

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.system_config import KEY_TTS, get_config

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


def _env_config() -> dict[str, Any]:
    """第二层：环境变量；第三层：代码默认值。"""
    return {
        "provider": os.getenv("TTS_PROVIDER", "indextts").lower(),
        # edge provider
        "edge_voice": os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural"),
        # openai 协议 provider（火山方舟/OpenAI/Azure 通用）
        "openai_api_key": os.getenv("OPENAI_TTS_API_KEY", ""),
        "openai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "openai_model": os.getenv("OPENAI_TTS_MODEL", "tts-1"),
        "openai_voice": os.getenv("OPENAI_TTS_VOICE", "alloy"),
        # doubao provider（Coding Plan HTTP POST）
        "doubao_api_key": os.getenv("DOUBAO_TTS_API_KEY", os.getenv("DOUBAO_TTS_TOKEN", "")),
        "doubao_token": os.getenv("DOUBAO_TTS_TOKEN", ""),
        "doubao_app_id": os.getenv("DOUBAO_TTS_APP_ID", ""),
        "doubao_voice": os.getenv("DOUBAO_TTS_VOICE", "BV001_streaming"),
        "doubao_resource_id": os.getenv("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0"),
        # local provider（macOS say + ffmpeg）
        "local_voice": os.getenv("TTS_LOCAL_VOICE", "Tingting"),
        "ffmpeg_bin": os.getenv("FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg"),
        # indextts provider（oMLX /v1/audio/speech + ref_audio 零样本克隆）
        "indextts_base_url": os.getenv("INDEXTTS_BASE_URL", "http://127.0.0.1:8008/v1"),
        "indextts_model": os.getenv("INDEXTTS_MODEL", "IndexTTS-1.5"),
        "indextts_ref_audio": os.getenv("INDEXTTS_REF_AUDIO", ""),
        "indextts_ref_text": os.getenv("INDEXTTS_REF_TEXT", ""),
    }


def resolve_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """DB > env > 默认值。override 里 None/空串视为「没配」，回落到下一层。"""
    config = _env_config()
    for field, value in (override or {}).items():
        if value not in (None, ""):
            config[field] = value
    return config


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
            base_url=config.get("indextts_base_url") or "http://127.0.0.1:8008/v1",
            model=config.get("indextts_model") or "IndexTTS-1.5",
            ref_audio_path=config.get("indextts_ref_audio") or "",
            ref_text=config.get("indextts_ref_text") or "",
        )
    raise ValueError(
        f"unsupported tts provider: {provider!r} " f"(supported: edge/openai/doubao/local/indextts)"
    )


def get_tts_client() -> TTSClient:
    """根据配置返回对应 client（同步，保持旧调用点不变）。

    CP TTS-Config 修复：兼容两层行为——
    1. 启动后没调过 reload() → 走 env/默认值建 client 并缓存；
    2. 调过 reload() → 用 DB 配置生效（覆盖 env）。
    """
    global _client, _signature
    if _client is None:
        config = resolve_config(None)
        _client = build_client(config)
        _signature = _signature_of(config)
    return _client


async def reload() -> TTSClient:
    """重读配置（Redis 5s 缓存挡 DB 压力），配置变了才重建 client。

    PUT /api/v1/admin/tts/config 写完会立刻 await reload() → 下一次蒸馏
    直接用新 client，不需要重启进程。
    """
    global _client, _signature
    config = resolve_config(await get_config(KEY_TTS))
    signature = _signature_of(config)
    if _client is None or signature != _signature:
        _client = build_client(config)
        _signature = signature
        log.info(
            "tts_client_reloaded",
            provider=config.get("provider"),
            api_key_set=bool(config.get("openai_api_key") or config.get("doubao_api_key")),
        )
    return _client


async def current_config() -> dict[str, Any]:
    """当前生效的完整配置（DB > env），不建 client。"""
    return resolve_config(await get_config(KEY_TTS))


def _signature_of(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)
