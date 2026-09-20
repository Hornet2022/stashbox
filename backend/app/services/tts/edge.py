"""Edge TTS 客户端（CP8.3 stub 真实化）。"""

import io
import logging

from .base import TTSClient

log = logging.getLogger(__name__)


class EdgeTTSClient(TTSClient):
    """Edge TTS 客户端（CP8.3 stub → 真实可用）。

    Edge TTS 免费但需要 edge-tts Python 包。无需任何 API key。

    依赖:pip install edge-tts  (PyPI: https://pypi.org/project/edge-tts/)

    默认音色:zh-CN-XiaoxiaoNeural(中文女声,听匣主力)。
    其他可选:zh-CN-YunxiNeural(男声), zh-CN-YunyangNeural(新闻男声)
    """

    DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

    def __init__(self, voice: str = DEFAULT_VOICE):
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
        """真实合成:调 edge_tts.Communicate(text, voice).save() 拿 mp3 bytes。

        Args:
            text: 待合成文本(中文 0-5000 字)
            voice: 音色名(默认 zh-CN-XiaoxiaoNeural)
            output_format: 仅支持 mp3(Edge 不支持其他格式)

        Returns:
            mp3 bytes

        Raises:
            RuntimeError: edge-tts 包未装
            Exception: 网络错误 / 文本超长
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        try:
            import edge_tts  # noqa: F401
        except ImportError as e:
            raise RuntimeError("edge-tts 未安装。请先 pip install edge-tts") from e

        if output_format != "mp3":
            log.warning(f"Edge TTS 仅支持 mp3, 收到 {output_format}, 强制使用 mp3")

        chosen_voice = voice or self.voice
        log.info(f"EdgeTTS synthesize: voice={chosen_voice}, text_len={len(text)}")

        # edge_tts.Communicate 是同步协程,直接 await
        communicate = edge_tts.Communicate(text=text, voice=chosen_voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        mp3_bytes = buf.getvalue()

        if not mp3_bytes:
            raise RuntimeError(f"Edge TTS 返回空 bytes(text_len={len(text)}, voice={chosen_voice})")

        log.info(f"EdgeTTS 合成完成: {len(mp3_bytes)} bytes")
        return mp3_bytes
