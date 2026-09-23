"""OpenAI 协议 TTS 客户端（CP8.3 真实化）。

这是一个**通用 OpenAI 兼容 TTS 客户端**，可用于：
- 火山方舟 ARK：base_url=https://ark.cn-beijing.volces.com/api/v3，ARK 平台开通 Doubao TTS 模型后取 API Key
- OpenAI 官方：base_url=https://api.openai.com/v1
- Azure OpenAI：base_url=https://{resource}.openai.azure.com/openai/deployments/{deployment}
- 其他 OpenAI-compatible TTS 端点

请求体（OpenAI 兼容）::

    POST {base_url}/audio/speech
    Authorization: Bearer <api_key>
    {
        "model": "doubao-tts",     # provider-specific 模型标识
        "voice": "BV001_streaming", # provider-specific 音色 ID
        "input": "...",
        "response_format": "mp3",
        "speed": 1.0
    }

响应：raw audio bytes（Content-Type: audio/mpeg）。

火山引擎方舟 TTS 模型示例（2026-09-21 官方文档）：
- 模型：doubao-seed-tts（或按控制台开通的实际模型名）
- 音色 ID：BV001_streaming 等（控制台 → 音色库 → 复制 speaker）
- 端点：https://ark.cn-beijing.volces.com/api/v3/audio/speech
- 鉴权：Bearer <ARK_API_KEY>（方舟控制台 → API Key 管理）

依赖: httpx（已在 requirements.txt）
凭证: OPENAI_TTS_API_KEY（与 LLM 端的 OPENAI_LLM_API_KEY 拆开，CP9.x 决策）
"""

import logging
import os
from typing import Any

import httpx

from .base import TTSClient

log = logging.getLogger(__name__)


# OpenAI 默认 base_url（覆写后可指向任意 OpenAI 兼容 TTS 端点）
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "alloy"


class OpenAITTSClient(TTSClient):
    """通用 OpenAI 协议 TTS 客户端（HTTP POST → audio bytes）。

    通过 OPENAI_BASE_URL 切换底层 provider：
      - 火山方舟：https://ark.cn-beijing.volces.com/api/v3，model=doubao-seed-tts
      - OpenAI：https://api.openai.com/v1，model=tts-1/tts-1-hd
      - Azure：https://{r}.openai.azure.com/openai/deployments/{dep}，model 用 deployment 名
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ):
        # 优先入参；入参缺省时从环境变量读（pydantic Settings 不会接管 .env 加载到这里）
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_TTS_API_KEY", "")
        self.model = model if model is not None else os.getenv("OPENAI_TTS_MODEL", _DEFAULT_MODEL)
        self.voice = voice if voice is not None else os.getenv("OPENAI_TTS_VOICE", _DEFAULT_VOICE)
        self.base_url = (
            base_url if base_url is not None else os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,  # 忽略沙箱/系统代理
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def provider_name(self) -> str:
        # 在工厂里叫 "openai"；底层真实 provider 看 base_url
        return "openai"

    def _endpoint(self) -> str:
        return f"{self.base_url}/audio/speech"

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
    ) -> bytes:
        """合成文本为音频字节（OpenAI 协议 /audio/speech）。

        Args:
            text: 待合成文本（蒸馏后的"听感版本"）
            voice: 音色 ID（provider-specific；None 走构造时设置的 self.voice）
            output_format: 输出格式（默认 mp3，OpenAI 还支持 opus/aac/flac）

        Returns:
            拼接后的音频 bytes

        Raises:
            ValueError: text 为空 / api_key 缺失
            RuntimeError: HTTP 异常 / 端点返回错误
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")
        if not self.api_key:
            raise RuntimeError(
                "OpenAITTSClient 需要 api_key。"
                "火山方舟：填 ARK API Key；OpenAI：填 sk-...；Azure：填 api key。"
            )

        body: dict[str, Any] = {
            "model": self.model,
            "voice": voice or self.voice,
            "input": text,
            "response_format": output_format,
        }

        log.info(
            "OpenAITTS synthesize: provider=%s model=%s voice=%s bytes=%d",
            self.base_url,
            self.model,
            body["voice"],
            len(text.encode("utf-8")),
        )

        try:
            resp = await self._client.post(self._endpoint(), json=body)
        except httpx.TimeoutException as e:
            raise RuntimeError(f"OpenAITTS 超时: {e}") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"OpenAITTS HTTP 异常: {e}") from e

        if resp.status_code >= 400:
            # 服务端一般会返 JSON 错误体；尝试解析
            err_body: Any
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text[:500]
            raise RuntimeError(f"OpenAITTS 错误 {resp.status_code}: {err_body}")

        audio = resp.content
        if not audio:
            raise RuntimeError(
                f"OpenAITTS 返回空音频（{resp.status_code}, content-type={resp.headers.get('content-type')}）"
            )

        log.info("OpenAITTS synthesized: %d bytes", len(audio))
        return audio

    async def close(self) -> None:
        await self._client.aclose()
