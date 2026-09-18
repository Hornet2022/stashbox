"""TTS client 抽象接口。"""

from abc import ABC, abstractmethod


class TTSClient(ABC):
    """TTS 语音合成客户端抽象。

    实现类：
    - MockTTSClient（开发/CI 用，生成静音或固定文本的 mp3）
    - EdgeTTSClient（生产，免费，留接口）
    - OpenAITTSClient（生产，$15/1M char，留接口）
    """

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice: str = "zh-CN-XiaoxiaoNeural",
        output_format: str = "mp3",
    ) -> bytes:
        """合成文本为音频字节。

        Args:
            text: 要合成的文本（蒸馏后的"听感版本"）
            voice: 语音 ID
            output_format: 输出格式（mp3/m4a/wav）

        Returns:
            音频字节流

        Raises:
            Exception: 合成失败
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 名（mock/edge/openai）。"""
        pass
