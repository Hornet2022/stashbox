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

⚠️ **长稿必须分段合成**（2026-10-21 生产实测踩过）：

    1503B / ~500 字  → 40s  ✅
    1636B / ~545 字  → 41s  ✅
    3142B / ~1047 字 → 60s  ❌ 超时失败（httpx 固定 60s 墙钟）

合成耗时随字数近似线性增长，而超时是**固定值** → 长稿必然击穿。
表现是：LLM 改写正常、script_text 也写进库了，但音频没出来，
文章卡在「蒸馏失败」，用户看到的稿子是对的、音频却是空的。

所以这里按句读边界切块 → 逐块合成 → 拼接（见 audio_chunks.py）。
IndexTTS provider 早就因为同类问题做了分段，这套实现现在共用。

⚠️ **分段要串行，不要并发**（同一天实测，3 段 × ~280 字）：

    并发=1 → 总耗时 54.2s，每段 17.4 / 37.6 / 54.1s
    并发=2 → 总耗时 51.1s，每段 16.4 / 36.2 / 51.1s
    并发=4 → 总耗时 53.4s，每段 53.4 / 20.9 / 34.2s

三种并发总耗时几乎一样 → **上游是串行处理的**，并发对它零吞吐贡献。
但并发 4 时最后一段要等 ~53s 才轮到处理，而 httpx 的读超时是**从请求
发出开始**算的 → 4 段并发时最后一段必然撞 60s。生产实跑确实这么炸过：
第 4 段在 60s 超时，整篇蒸馏失败（前 3 段都成功了）。

串行则每段 17s 完成，对 60s 超时有 3.5 倍余量，总耗时不变。

火山引擎方舟 TTS 模型示例（2026-09-21 官方文档）：
- 模型：doubao-seed-tts（或按控制台开通的实际模型名）
- 音色 ID：BV001_streaming 等（控制台 → 音色库 → 复制 speaker）
- 端点：https://ark.cn-beijing.volces.com/api/v3/audio/speech
- 鉴权：Bearer <ARK_API_KEY>（方舟控制台 → API Key 管理）

依赖: httpx（已在 requirements.txt）
凭证: OPENAI_TTS_API_KEY（与 LLM 端的 OPENAI_LLM_API_KEY 拆开，CP9.x 决策）
"""

import asyncio
import logging
import os
from typing import Any

import httpx

from .audio_chunks import concat_audio, split_into_chunks
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
        chunk_chars: int | None = None,
        chunk_concurrency: int | None = None,
    ):
        # 优先入参；入参缺省时从环境变量读（pydantic Settings 不会接管 .env 加载到这里）
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_TTS_API_KEY", "")
        self.model = model if model is not None else os.getenv("OPENAI_TTS_MODEL", _DEFAULT_MODEL)
        self.voice = voice if voice is not None else os.getenv("OPENAI_TTS_VOICE", _DEFAULT_VOICE)
        self.base_url = (
            base_url if base_url is not None else os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        # 单次请求的最大字数。超时 60s 时，~280 字是实测安全线（17.4s），
        # 1047 字必然超时 —— 取 300 留足余量（见文件头的实测记录）。
        self.chunk_chars = (
            chunk_chars
            if chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_CHUNK_CHARS", "300"))
        )
        # 分段并行度。**默认 1（串行）** —— 不是为了性能，是为了让每一段的
        # 计时器从「真正开始被上游处理」起算。
        #
        # 实测（2026-10-21，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base，3 段 × ~280 字）：
        #     并发=1 → 总耗时 54.2s，每段 17.4 / 37.6 / 54.1s
        #     并发=2 → 总耗时 51.1s，每段 16.4 / 36.2 / 51.1s
        #     并发=4 → 总耗时 53.4s，每段 53.4 / 20.9 / 34.2s
        #
        # 三种并发**总耗时几乎一样** → 上游是串行处理的，并发对它零吞吐贡献。
        # 但并发 4 时最后一段要等 ~53s 才轮到（前面 3 段占着），
        # 而 httpx 的超时是**从请求发出开始**算的读超时 → 4 段以上必然撞 60s。
        #
        # 生产上也确实这么炸过：4 段并发实跑，第 4 段在 60s 超时，
        # 整篇蒸馏失败（19:30:14 发出 → 19:31:14 超时，前 3 段都成功了）。
        #
        # 串行则每段 17s 就完成，对 60s 超时有 3.5 倍余量；
        # 总耗时不变（上游本来就串行），服务器（1.6GB / 2 vCPU）还更省资源。
        self.chunk_concurrency = (
            chunk_concurrency
            if chunk_concurrency is not None
            else int(os.getenv("OPENAI_TTS_CONCURRENCY", "1"))
        )
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

        长文本会**自动分段**（按句读边界切块 → 并行合成 → 拼接），
        原因见文件头的实测记录：整篇合成在 ~1000 字时会撞上固定超时。

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

        if self.chunk_chars <= 0 or len(text) <= self.chunk_chars:
            return await self._synthesize_once(text, voice, output_format)

        # ── 长稿：切块 → 逐块合成 → 拼接 ──────────────────────────────
        #
        # ⚠️ 为什么不直接调高 timeout：合成耗时随字数近似线性增长，
        # 而「多久算超时」得跟着稿长变 —— 与其猜一个上限（猜小了长稿失败，
        # 猜大了真卡死的请求要等很久才被发现），不如把单次请求压到
        # 已知安全的长度（~280 字 / 17s 实测），这样**每次请求都用同一个
        # 已知安全的超时**，行为可预测。
        #
        # ⚠️ concurrency 默认 1（串行），原因见 __init__ 里的实测记录：
        # 上游串行处理，并发零收益，却会让末段在队列里等到逼近超时。
        chunks = split_into_chunks(text, self.chunk_chars)
        log.info(
            "OpenAITTS 分段合成: total_chars=%d chunks=%d chunk_chars<=%d concurrency=%d",
            len(text),
            len(chunks),
            self.chunk_chars,
            self.chunk_concurrency,
        )

        sem = asyncio.Semaphore(max(1, self.chunk_concurrency))

        async def _one(chunk: str) -> bytes:
            async with sem:
                return await self._synthesize_once(chunk, voice, output_format)

        # gather 保序（返回顺序 = 输入顺序），拼接顺序才不会乱
        pieces = await asyncio.gather(*[_one(c) for c in chunks])
        merged = concat_audio(list(pieces))
        log.info("OpenAITTS 分段合成完成: chunks=%d bytes=%d", len(pieces), len(merged))
        return merged

    async def _synthesize_once(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
    ) -> bytes:
        """单次请求合成（不分段）。分段逻辑在 synthesize() 里。"""
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
