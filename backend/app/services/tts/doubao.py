"""火山引擎豆包 TTS 客户端（CP9.x：HTTP POST Coding Plan 版）。

端点: https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional
鉴权: HTTP 头 X-Api-Key + X-Api-Resource-Id: seed-tts-2.0

请求体::

    {
        "user": {"uid": "..."},
        "req_params": {
            "text": "...",
            "speaker": "zh_female_xxx",   # 来自控制台 → 音色库
            "audio_params": {
                "format": "mp3",          # mp3 / ogg_opus / pcm
                "sample_rate": 24000      # 8000~48000
            }
        }
    }

响应：HTTP Chunked 流式 JSON，每块形如::

    {"code": 0, "message": "", "data": "<base64 音频分片>"}

或单次完整 JSON（按接口和参数选择）。

依赖: httpx（已在 requirements.txt）
凭证: DOUBAO_TTS_API_KEY（DOUBAO_TTS_APP_ID/TOKEN 旧版兼容，自动 fallback）
"""

import base64
import json
import logging
import os

import httpx

from .base import TTSClient

log = logging.getLogger(__name__)


class DoubaoTTSClient(TTSClient):
    """火山引擎豆包 TTS 客户端（Coding Plan X-Api-Key 鉴权，HTTP POST）。"""

    HTTP_ENDPOINT = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
    DEFAULT_VOICE = "BV001_streaming"  # 旧版默认；Coding Plan 推荐用 zh_female_*_bigtts
    DEFAULT_RESOURCE_ID = "seed-tts-2.0"
    DEFAULT_SAMPLE_RATE = 24000

    def __init__(
        self,
        app_id: str | None = None,  # 旧版兼容（X-Api-App-Id）
        token: str | None = None,  # 旧版兼容（X-Api-Access-Key）
        api_key: str | None = None,  # 新版：Coding Plan 专属 API Key
        voice: str | None = None,
        resource_id: str | None = None,
        timeout: float = 60.0,
    ):
        # CP9.x：优先 Coding Plan 专属 API Key；旧版 APP_ID/TOKEN 作 fallback
        self.api_key = (
            api_key
            if api_key is not None
            else (
                token
                if token
                else os.getenv("DOUBAO_TTS_API_KEY", os.getenv("DOUBAO_TTS_TOKEN", ""))
            )
        )
        self.app_id = app_id  # 旧版专用
        self.voice = (
            voice if voice is not None else os.getenv("DOUBAO_TTS_VOICE", self.DEFAULT_VOICE)
        )
        self.resource_id = (
            resource_id
            if resource_id is not None
            else os.getenv("DOUBAO_TTS_RESOURCE_ID", self.DEFAULT_RESOURCE_ID)
        )
        # trust_env=False：忽略沙箱/系统 HTTP(S)_PROXY（代理端口轮换会导致外网请求随机 ConnectError）
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    @property
    def provider_name(self) -> str:
        return "doubao"

    def _ensure_credentials(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "DoubaoTTSClient 需要 Coding Plan 专属 API Key。"
                "从火山方舟 Coding Plan 控制台获取后填到 backend/.env 的 DOUBAO_TTS_API_KEY。"
                "旧版 APP_ID/TOKEN 已不推荐，保留兼容。"
            )
        return self.api_key

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
    ) -> bytes:
        """合成文本为 mp3 bytes（Coding Plan HTTP POST 单向流式）。

        Args:
            text: 待合成文本（蒸馏后的"听感版本"）
            voice: 音色 ID（None 走构造时设置的 self.voice，建议从控制台→音色库 复制）
            output_format: 输出格式 mp3 / ogg_opus / pcm（默认 mp3）

        Returns:
            拼接后的音频 bytes

        Raises:
            ValueError: text 为空
            RuntimeError: 缺凭证 / HTTP 异常 / 服务端返回非 0 code / 无音频
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        api_key = self._ensure_credentials()
        chosen_voice = voice or self.voice

        body = {
            "user": {"uid": "tingxia-user"},
            "req_params": {
                "text": text,
                "speaker": chosen_voice,
                "audio_params": {
                    "format": output_format,
                    "sample_rate": self.DEFAULT_SAMPLE_RATE,
                },
            },
        }

        headers = {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": __import__("uuid").uuid4().hex,
            "Content-Type": "application/json",
        }

        log.info(
            "DoubaoTTS synthesize: voice=%s format=%s text_bytes=%d resource_id=%s",
            chosen_voice,
            output_format,
            len(text.encode("utf-8")),
            self.resource_id,
        )

        try:
            resp = await self._client.post(self.HTTP_ENDPOINT, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise RuntimeError(f"Doubao TTS 超时: {e}") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"Doubao TTS HTTP 异常: {e}") from e

        if resp.status_code >= 400:
            raise RuntimeError(f"Doubao TTS HTTP {resp.status_code}: {resp.text[:500]}")

        # 响应可能是 ①单 JSON 对象 data=base64 ②Chunked JSON 流 ④完整 audio bytes
        # 先按 JSON 解析；失败再当 raw audio
        try:
            audio = self._collect_audio_chunks_from_json(resp.text)
        except (json.JSONDecodeError, ValueError, KeyError):
            # 不是 JSON，按 raw bytes 处理
            audio = resp.content

        if not audio:
            raise RuntimeError(
                f"Doubao TTS 返回空音频（{resp.status_code}, content-type={resp.headers.get('content-type')}）"
            )

        log.info("DoubaoTTS synthesized: %d bytes", len(audio))
        return audio

    @staticmethod
    def _collect_audio_chunks_from_json(text: str) -> bytes:
        """把 HTTP 响应解析成 base64 解码后的音频字节。

        支持 3 种响应形态：
        1) 单 JSON: {"code": 0, "data": "<base64>"}         (V3 标准)
        2) 单 JSON: {"code": 20000000, "message": "OK", "data": ...}  (Coding Plan)
        3) 多行 JSON（每行一个 chunk；HTTP Chunked 流）
        4) JSON 数组: [{"code": ..., "data": "..."}, ...]

        火山引擎不同端点用不同成功码：
        - V3 标准端点：code=0 视为成功
        - Coding Plan 端点：code=20000000 视为成功
        其他非 0 值（如 4xxxxxxx）为错误。
        """
        text = text.strip()
        if not text:
            return b""

        # 尝试 JSON 数组
        if text.startswith("["):
            items = json.loads(text)
            return b"".join(base64.b64decode(item["data"]) for item in items if item.get("data"))

        # 尝试单 JSON 对象 / 多行 JSON 流
        if text.startswith("{"):
            chunks_out = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                code = obj.get("code", 0)
                # 火山引擎约定：code=0 或 code=20000000 都视为成功
                if code not in (0, 20000000):
                    raise RuntimeError(
                        f"Doubao TTS 错误: code={code} message={obj.get('message')!r}"
                    )
                data = obj.get("data")
                if data:
                    chunks_out.append(base64.b64decode(data))
            return b"".join(chunks_out)

        # 既不是数组也不是对象 → 不是 JSON
        raise json.JSONDecodeError("response is not JSON", text, 0)

    async def close(self) -> None:
        await self._client.aclose()
