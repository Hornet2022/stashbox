"""IndexTTS provider（oMLX OpenAI 兼容 /v1/audio/speech + 零样本音色克隆）。

服务：macmini 上 oMLX CLI serve 同时托管 embedding / OCR / IndexTTS-1.5。
端点契约（已实测 2026-09-22）：

    POST {base_url}/audio/speech
    {
      "model": "IndexTTS-1.5",
      "input": "待合成文本",
      "ref_audio": "<参考音频 wav 的 base64>",
      "ref_text": "参考音频的文字转录"
    }
    → 200 + audio/wav bytes（16-30s 文本约 10-20s 合成）

缺 ref_audio/ref_text 服务返回 400（"ref_text is required when ref_audio is provided"），
所以两者必须成对配置，缺一不可。

与 doubao/openai provider 的区别：
- voice 参数无意义 —— 音色由参考音频决定（克隆），保留形参只为契约兼容
- 返回是 WAV（RIFF），交给 step4/pipeline 落 .wav/.m4a 均能被 ExoPlayer 播
"""

import asyncio
import base64
import logging
import os
from pathlib import Path

import httpx

from .base import TTSClient

log = logging.getLogger(__name__)


class IndexTTSError(RuntimeError):
    """IndexTTS 合成失败（网络 / 服务 4xx-5xx / 空音频）。"""


class IndexTTSClient(TTSClient):
    """oMLX 托管 IndexTTS-1.5（OpenAI 兼容 + ref_audio 零样本克隆）。"""

    DEFAULT_BASE_URL = "http://127.0.0.1:8008/v1"
    DEFAULT_MODEL = "IndexTTS-1.5"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        ref_audio_path: str | None = None,
        ref_text: str | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.getenv("INDEXTTS_BASE_URL", self.DEFAULT_BASE_URL)).rstrip(
            "/"
        )
        self.model = model or os.getenv("INDEXTTS_MODEL", self.DEFAULT_MODEL)
        self.ref_audio_path = ref_audio_path or os.getenv("INDEXTTS_REF_AUDIO", "")
        self.ref_text = ref_text or os.getenv("INDEXTTS_REF_TEXT", "")
        self.timeout = timeout
        # trust_env=False：忽略沙箱/系统 HTTP(S)_PROXY（代理漂移会打挂本地 127.0.0.1 请求）
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        # 参考音频 base64 缓存（文件不重新读盘，除非 ref_audio_path 变化）
        self._ref_b64: str | None = None
        self._ref_b64_for: str | None = None

    @property
    def provider_name(self) -> str:
        return "indextts"

    # -- 参考音频 ---------------------------------------------------------
    def _load_ref_audio_b64(self) -> str:
        """读参考 wav → base64（缓存，路径变更才重读）。"""
        path = self.ref_audio_path
        if not path:
            raise IndexTTSError(
                "IndexTTS 需要参考音频：配置 INDEXTTS_REF_AUDIO（管理后台 TTS 设置页 indextts_ref_audio）。"
            )
        p = Path(path).expanduser()
        if not p.is_file():
            raise IndexTTSError(f"参考音频文件不存在: {p}")
        if self._ref_b64 is not None and self._ref_b64_for == str(p):
            return self._ref_b64
        data = p.read_bytes()
        if len(data) < 1000:
            raise IndexTTSError(f"参考音频太小({len(data)}B)，可能不是有效 wav: {p}")
        self._ref_b64 = base64.b64encode(data).decode("ascii")
        self._ref_b64_for = str(p)
        log.info("IndexTTS ref audio loaded: %s (%dB)", p, len(data))
        return self._ref_b64

    def _ensure_ref_text(self) -> str:
        if not self.ref_text.strip():
            raise IndexTTSError(
                "IndexTTS 需要参考音频转录文本：配置 INDEXTTS_REF_TEXT"
                "（管理后台 TTS 设置页 indextts_ref_text，须与 ref_audio 内容一致）。"
            )
        return self.ref_text

    # -- 合成 -------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        voice: str | None = None,  # 契约兼容；IndexTTS 音色由 ref_audio 决定
        output_format: str = "wav",
    ) -> bytes:
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        ref_b64 = self._load_ref_audio_b64()
        ref_text = self._ensure_ref_text()

        payload = {
            "model": self.model,
            "input": text.strip(),
            "ref_audio": ref_b64,
            "ref_text": ref_text,
        }
        url = f"{self.base_url}/audio/speech"
        log.info(
            "IndexTTS synthesize: model=%s text_chars=%d ref=%s",
            self.model,
            len(text),
            self.ref_audio_path,
        )
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.TimeoutException as e:
            raise IndexTTSError(f"IndexTTS 合成超时({self.timeout}s): {e}") from e
        except httpx.HTTPError as e:
            raise IndexTTSError(f"IndexTTS HTTP 异常: {e}") from e

        if resp.status_code >= 400:
            raise IndexTTSError(f"IndexTTS HTTP {resp.status_code}: {resp.text[:400]}")

        audio = resp.content
        ct = resp.headers.get("content-type", "")
        # oMLX 正常返回 audio/wav；若哪天返回 JSON base64 兜底解一下
        if ct.startswith("application/json"):
            import json as _json

            try:
                obj = _json.loads(resp.text)
                data = obj.get("data") or obj.get("audio")
                if data:
                    audio = base64.b64decode(data)
            except Exception:
                pass
        if not audio or len(audio) < 100:
            raise IndexTTSError(f"IndexTTS 返回空音频（status={resp.status_code}, ct={ct}）")
        log.info("IndexTTS synthesized: %d bytes (%s)", len(audio), ct)
        return audio

    async def close(self) -> None:
        await self._client.aclose()
