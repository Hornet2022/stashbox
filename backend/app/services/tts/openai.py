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

⚠️ **上游会抖动，单块超时 + 单块重试**（同一天实测）：

    段1 16s ✅   段2 60s 超时 ❌   段3 16s ✅   ← 同一篇稿子的三段

所以超时从 60s 放宽到 120s（正常段 13~20s，6 倍余量），
并且单块失败时**只重试这一块**（OPENAI_TTS_CHUNK_RETRIES，默认 2，
退避 3s/6s）—— 不必让 Arq 把整篇（LLM 改写 + 全部分块）重来一遍。

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

# 「输出疑似被静默截断」的语速阈值（字/秒）。
#
# 正常中文播报 **3.88~4.61 字/秒**（实测 100/150/200/250/400 字五档）；
# 端点只念了开头时，字数没少、时长偏短 → 语速虚高。
#
# 实测到的真截断样本：290 字只产出 40.6s → 语速 7.14
# （按 4.3 字/秒本该 67.4s，**丢了约 40%**，约 115 字）。
#
# 取 5.5：离正常上界 4.61 有 19% 余量，又离真截断 7.14 有 23% 距离。
# 宁可误报（多合成一次）也不要漏报（内容缺失但状态成功）。
#
# 判据与 IndexTTS 的 _TRUNCATION_CHARS_PER_SEC 同源（那里正常 4.41~4.63、
# 真截断 8.35、阈值 7.0）。这里实测的正常区间更窄，所以阈值更严。
_TRUNCATION_CHARS_PER_SEC = 5.5

# 二分自愈的最大递归深度（2^6 = 64 段，兜底防无限递归）。
_MAX_SPLIT_DEPTH = 6


class _TruncatedOutput(RuntimeError):
    """端点返回了 HTTP 200 但内容被静默截断（只念了开头）。"""


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
        timeout: float | None = None,
        chunk_chars: int | None = None,
        chunk_concurrency: int | None = None,
        chunk_retries: int | None = None,
        min_chunk_chars: int | None = None,
    ):
        # 优先入参；入参缺省时从环境变量读（pydantic Settings 不会接管 .env 加载到这里）
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_TTS_API_KEY", "")
        self.model = model if model is not None else os.getenv("OPENAI_TTS_MODEL", _DEFAULT_MODEL)
        self.voice = voice if voice is not None else os.getenv("OPENAI_TTS_VOICE", _DEFAULT_VOICE)
        self.base_url = (
            base_url if base_url is not None else os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = (
            timeout if timeout is not None else float(os.getenv("OPENAI_TTS_TIMEOUT", "120"))
        )
        # 单块失败时的重试次数（只重试**这一块**，不是整篇重来）。
        #
        # 实测（2026-10-21）：上游会**抖动** —— 同一篇稿子的 3 段里，
        # 段1 16s ✅、段2 60s 超时 ❌、段3 16s ✅，Arq 重试后整篇三段全通。
        # 单块重试能把「某段抖动」消化在本地，不必让 Arq 把整篇
        # （LLM 改写 + 全部分块）重来一遍 —— 那是双倍的钱和双倍的时间。
        self.chunk_retries = (
            chunk_retries
            if chunk_retries is not None
            else int(os.getenv("OPENAI_TTS_CHUNK_RETRIES", "2"))
        )
        # 自愈的最小粒度：块被细到这么小还截断，就不是长度问题（真失败）。
        self.min_chunk_chars = (
            min_chunk_chars
            if min_chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_MIN_CHUNK_CHARS", "60"))
        )
        # 段间静音秒数（0 = 不插）。默认 0.3s ≈ 一次自然换气。
        self.gap_sec = float(os.getenv("OPENAI_TTS_GAP_SEC", "0.3"))
        # 单次请求的最大字数。
        #
        # 取 200 的依据（2026-10-21 实测，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）：
        #     100 字 → 25.2s  语速 3.97  ✅
        #     150 字 → 32.5s  语速 4.61  ✅
        #     200 字 → 46.5s  语速 4.30  ✅  ← 取这个
        #     250 字 → 64.5s  语速 3.88  ✅ 但已越过上游 60s 线
        #     300 字 → 504 上游语音合成超时(60s)
        #     400 字 → 94.5s  语速 4.23  ✅（能过但 46s 时长没保障）
        #
        # 上游**自己**也有 60s 墙钟（返回 504 backend_timeout「上游语音合成超时」），
        # 且是波动的（250 字成功、300 字 504、400 字又成功）。200 字实测 46.5s，
        # 离那条线有 23% 余量。
        #
        # 之前用 300 字 → 撞上两个问题：① 上游 504 ② 端点静默截断
        # （290 字只念了 60%，见 _TRUNCATION_CHARS_PER_SEC）。
        self.chunk_chars = (
            chunk_chars
            if chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_CHUNK_CHARS", "200"))
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
            timeout=self.timeout,
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
            return await self._synthesize_chunk(text, voice, output_format)

        # ── 长稿：切块 → 逐块合成 → 拼接 ──────────────────────────────
        #
        # ⚠️ 为什么不直接调高 timeout：合成耗时随字数近似线性增长，
        # 而「多久算超时」得跟着稿长变 —— 与其猜一个上限（猜小了长稿失败，
        # 猜大了真卡死的请求要等很久才被发现），不如把单次请求压到
        # 已知安全的长度，这样**每次请求都用同一个已知安全的超时**，
        # 行为可预测。
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
                last_exc: Exception | None = None
                for attempt in range(1, self.chunk_retries + 2):
                    try:
                        return await self._synthesize_chunk(chunk, voice, output_format)
                    except Exception as exc:
                        last_exc = exc
                        if attempt > self.chunk_retries:
                            break
                        # 上游抖动是**间歇性**的：实测同一篇稿子的 3 段里，
                        # 有一段 60s 超时而另外两段 16s 就好了。
                        # 重试前先等一下，别在同一个坏窗口里连打。
                        wait = 3.0 * attempt
                        log.warning(
                            "OpenAITTS 分块失败，重试: attempt=%s/%s wait=%ss err=%s",
                            attempt,
                            self.chunk_retries,
                            wait,
                            exc,
                        )
                        await asyncio.sleep(wait)
                assert last_exc is not None
                raise last_exc

        # gather 保序（返回顺序 = 输入顺序），拼接顺序才不会乱
        pieces = list(await asyncio.gather(*[_one(c) for c in chunks]))
        merged = self._concat_with_gaps(pieces)
        log.info("OpenAITTS 分段合成完成: chunks=%d bytes=%d", len(pieces), len(merged))
        return merged

    def _concat_with_gaps(self, pieces: list[bytes]) -> bytes:
        """拼接各段，段间插入短静音（让听感像换气，而不是被切开的连续语音）。

        静音的采样率/声道从第一段探测 —— 必须与内容一致，否则 concat demuxer
        会重采样，`-c copy` 就不成立了。

        拼完**自检**总时长：与「各段之和 + 静音时长」差太多就回退到不加静音
        的版本（拼接出错时宁可少个停顿，也不能丢内容）。
        """
        if len(pieces) < 2 or self.gap_sec <= 0:
            return concat_audio(pieces)

        from stashbox.backend.app.services.tts.audio_chunks import silence_mp3
        from stashbox.backend.app.services.tts.audio_duration import audio_duration_sec, mp3_info

        info = mp3_info(pieces[0])
        if not info:
            return concat_audio(pieces)
        _, rate, channels = info

        silence = silence_mp3(self.gap_sec, rate, channels)
        if not silence:
            return concat_audio(pieces)

        with_gaps: list[bytes] = []
        for i, piece in enumerate(pieces):
            if i:
                with_gaps.append(silence)
            with_gaps.append(piece)

        merged = concat_audio(with_gaps)

        # 自检：总时长应 ≈ 各段之和 + 静音段数 × gap
        expected = 0.0
        for piece in pieces:
            d = audio_duration_sec(piece)
            if d:
                expected += d
        expected += self.gap_sec * (len(pieces) - 1)
        actual = audio_duration_sec(merged) or 0
        if expected > 0 and abs(actual - expected) > 3:
            log.warning(
                "OpenAITTS 段间静音拼接异常，回退无静音版本: actual=%.1fs expected=%.1fs",
                actual,
                expected,
            )
            return concat_audio(pieces)
        return merged

    async def _synthesize_chunk(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
        depth: int = 0,
    ) -> bytes:
        """合成一块文本，**自带截断自愈**：被端点静默截断就自动二分重来。

        ## 为什么需要自愈

        这个端点会**静默截断**长输入：HTTP 200、音频合法，但只念了开头，
        后面的内容凭空消失（与 IndexTTS 的同类坑，见 indextts.py 顶部的实测）。

        实测（2026-10-21，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）：

            134 字 → 24.3s → 5.52 字/秒  ✅ 正常（中文播报 4~6 字/秒）
            290 字 → 40.6s → 7.14 字/秒  ⚠️ 截断（按 5.5 字/秒本该 52.7s，丢了 ~67 字）

        判据用**语速**（字/秒）：正常播报 4~6 字/秒，截断会让语速虚高。
        阈值 6.5 离正常上界有余量、又稳稳低于真截断的 7.14
        （与 IndexTTS 的 _TRUNCATION_CHARS_PER_SEC 同一判据，取值略严）。

        **只抛错是不够的** —— 同样大小的块重试还是会被截断。
        所以这里把被截断的块**按句读边界二分**后分别合成（内容更短 → 不会被截断），
        再拼回去。递归到 min_chunk_chars 仍截断才算真失败。
        """
        try:
            audio = await self._synthesize_once(text, voice, output_format)
            self._assert_not_truncated(text, audio)
            return audio
        except _TruncatedOutput:
            if len(text) <= self.min_chunk_chars or depth >= _MAX_SPLIT_DEPTH:
                raise
            # 按句读边界二分（复用切块工具，不把词切两半）
            parts = split_into_chunks(text, max(1, len(text) // 2))
            if len(parts) < 2:
                raise
            log.info(
                "OpenAITTS 分块被截断，二分重来: depth=%s chars=%d → %d 块",
                depth + 1,
                len(text),
                len(parts),
            )
            pieces = [
                await self._synthesize_chunk(part, voice, output_format, depth + 1)
                for part in parts
            ]
            return concat_audio(pieces)

    def _assert_not_truncated(self, text: str, audio: bytes) -> None:
        """语速校验：识别「HTTP 200 但内容被静默截断」。

        判据与 IndexTTS 的 _assert_not_truncated 同源（语速），
        但这里用的是 mp3，所以用 audio_duration.mp3_duration_sec 算时长。
        """
        from stashbox.backend.app.services.tts.audio_duration import audio_duration_sec

        dur = audio_duration_sec(audio)
        if not dur or dur <= 0:
            log.warning("OpenAITTS 音频时长不可解析，跳过截断校验")
            return
        cps = len(text) / dur
        if cps > _TRUNCATION_CHARS_PER_SEC:
            raise _TruncatedOutput(
                f"OpenAITTS 输出疑似被截断：{len(text)} 字只产出 {dur:.1f}s 音频"
                f"（{cps:.2f} 字/秒，正常应 < {_TRUNCATION_CHARS_PER_SEC}）"
            )

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
