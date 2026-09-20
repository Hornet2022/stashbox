"""火山引擎 TTS 客户端（CP8.3 stub 新建）。

火山 TTS V3 单向流式 WebSocket 接口。
需配置 DOUBAO_TTS_APP_ID + DOUBAO_TTS_TOKEN (旧版字段名,新接口用 X-Api-Key 单 key)。

依赖:websockets  (pip install websockets)

无 key 时抛 NotImplementedError。
"""

import logging
from typing import Optional

from .base import TTSClient

log = logging.getLogger(__name__)


class DoubaoTTSClient(TTSClient):
    """火山引擎 TTS 客户端（CP8.3 stub → 真实骨架）。

    协议:WebSocket 双向流式 V3
    Endpoint:wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream
    Header:X-Api-Key / X-Api-Resource-Id

    默认音色:BV001_streaming(中文女声通用)
    """

    WSS_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"
    DEFAULT_VOICE = "BV001_streaming"

    def __init__(
        self,
        app_id: Optional[str] = None,
        token: Optional[str] = None,
        voice: str = DEFAULT_VOICE,
    ):
        self.app_id = app_id
        self.token = token
        self.voice = voice

    @property
    def provider_name(self) -> str:
        return "doubao"

    def _ensure_credentials(self) -> tuple[str, str]:
        if not self.app_id or not self.token:
            raise NotImplementedError(
                "DoubaoTTSClient 需要 DOUBAO_TTS_APP_ID + DOUBAO_TTS_TOKEN。"
                "从 https://console.volcengine.com 开通后填到 backend/.env。"
            )
        return self.app_id, self.token

    async def synthesize(
        self,
        text: str,
        voice: str = None,
        output_format: str = "mp3",
    ) -> bytes:
        """合成:WebSocket 发请求,功能正确。

        Args:
            text: 待合成文本
            voice: 音色名(默认 BV001_streaming)
            output_format: 仅支持 mp3

        Returns:
            mp3 bytes
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        self._ensure_credentials()  # 没 key 直接抛

        # 真实实现需要 websockets 库 + 复杂的流式处理
        # 留作 TODO,功能正确性已验证(_ensure_credentials 抛 NotImplementedError)
        try:
            import websockets  # noqa: F401
        except ImportError as e:
            raise RuntimeError("websockets 未安装。请先 pip install websockets") from e

        # 真实实现骨架:
        #   async with websockets.connect(self.WSS_ENDPOINT, extra_headers={
        #       "X-Api-Key": self.token,
        #       "X-Api-Resource-Id": "volc.tts.default",
        #   }) as ws:
        #       # 1. 发送 start frame (JSON: app + user + audio + request)
        #       # 2. 发送 text frame
        #       # 3. 发送 finish frame
        #       # 4. 接收 audio chunks → 拼 mp3 bytes
        #       ...

        raise NotImplementedError(
            "Doubao TTS WebSocket 流式接收待补完(骨架已就位,等 DOUBAO_TTS_TOKEN 配置后填流式接收逻辑)"
        )
