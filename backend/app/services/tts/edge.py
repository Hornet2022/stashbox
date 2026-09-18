"""Edge TTS 客户端（生产用，留接口）。"""

from .base import TTSClient


class EdgeTTSClient(TTSClient):
    """Edge TTS 客户端（接口预留，未实现）。

    Edge TTS 免费但需要 edge-tts Python 包。

    切换到真 TTS 时：
    - pip install edge-tts
    - 实现 synthesize() 调 edge_tts.Communicate
    """

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.voice = voice

    @property
    def provider_name(self) -> str:
        return "edge"

    async def synthesize(
        self,
        text: str,
        voice: str = None,
        output_format: str = "mp3",
    ) -> bytes:
        raise NotImplementedError(
            "EdgeTTSClient 待实现。pip install edge-tts + 实现 synthesize()。"
        )
