"""TTS client 工厂。"""

import os
from .base import TTSClient
from .mock import MockTTSClient
from .edge import EdgeTTSClient
from .openai import OpenAITTSClient


def get_tts_client() -> TTSClient:
    """根据 settings.tts_provider 返回对应 client。"""
    provider = os.getenv("TTS_PROVIDER", "mock").lower()

    if provider == "mock":
        return MockTTSClient()
    elif provider == "edge":
        voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
        return EdgeTTSClient(voice=voice)
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        return OpenAITTSClient(api_key=api_key)
    else:
        return MockTTSClient()
