"""Mock TTS 客户端。生成固定 mp3（用最小有效 mp3 header）。"""

import asyncio
from .base import TTSClient


# 最小有效 mp3（静音 1 帧，约 100ms）
_MOCK_MP3 = bytes(
    [
        0xFF,
        0xFB,
        0x90,
        0x00,  # MPEG 1 Layer 3 header
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0xFF,
        0xFB,
        0x90,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
    ]
)


class MockTTSClient(TTSClient):
    """Mock TTS 客户端。

    不调任何外部 API，返回固定 mp3（静音）。
    用于本地开发 + CI 测试。
    """

    @property
    def provider_name(self) -> str:
        return "mock"

    async def synthesize(
        self,
        text: str,
        voice: str = "zh-CN-XiaoxiaoNeural",
        output_format: str = "mp3",
    ) -> bytes:
        # 模拟合成延迟
        await asyncio.sleep(0.3)

        # 返回静音 mp3（mock）
        # 生产实现应该用 Edge TTS / OpenAI TTS 生成真音频
        return _MOCK_MP3 * 100  # 约 100 帧 = 3 秒静音
