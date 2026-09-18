"""OpenAI TTS 客户端（生产用，留接口）。"""

from .base import TTSClient


class OpenAITTSClient(TTSClient):
    """OpenAI TTS 客户端（接口预留，未实现）。

    切换到真 TTS 时：
    - pip install openai
    - 实现 synthesize() 调 openai.AsyncClient.audio.speech.create
    """

    def __init__(self, api_key: str = "", model: str = "tts-1", voice: str = "alloy"):
        self.api_key = api_key
        self.model = model
        self.voice = voice

    @property
    def provider_name(self) -> str:
        return "openai"

    async def synthesize(
        self,
        text: str,
        voice: str = None,
        output_format: str = "mp3",
    ) -> bytes:
        raise NotImplementedError(
            "OpenAITTSClient 待实现。pip install openai + 实现 synthesize()。"
        )
