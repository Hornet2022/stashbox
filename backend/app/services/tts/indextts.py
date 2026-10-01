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

⚠️ **这个端点会静默截断长输入 —— 这是最关键的坑**（实测 2026-09-28）：

    150 字 → 音频 17.9s  语速  8.4 字/秒  ✅ 正常
    300 字 → 音频  6.9s  语速 43.7 字/秒  ❌ 截断
    600 字 → 音频 19.8s  语速 30.4 字/秒  ❌ 截断

请求会返回 **HTTP 200 + 合法 WAV**，但音频只有开头一小段，后面的内容凭空消失。
判定方法：语速超过 ~10 字/秒就是被截断了（正常中文播报 4~6 字/秒）。

所以 `synthesize()` 必须在 ~150 字处切块（`INDEXTTS_CHUNK_CHARS`），否则长稿会**静默丢内容**。

⚠️ **合成实时率约 0.6x**（不是"慢 30 倍"，别算错基准）：

    50/100/150/300/600 字 → RTF 全部稳定在 0.60~0.62x

即"生成 1 秒音频需要 1.6 秒计算"。GPU 本身完全正常（Metal 可用，M4，
matmul 实测 41 TFLOPS），瓶颈在模型结构（IndexTTS 的扩散/流匹配声码器要跑
多轮去噪），不是硬件或内存。

⚠️ **8000 与 8008 两个 oMLX 实例都会间歇性空转，而且会自愈**：
空转时 50% CPU、合成端点零响应（13 字短文本也挂起）。
根因是**两个实例同时驻留模型把内存打爆**（实测 swap 一度 14.9GB/15.4GB、
可用内存 17%，oMLX 各 8~11GB writable regions、50%+ 被换出）→ 推理退化成
磁盘换页，表现为"随机 20~40s"和"卡死"。
**只保留一个实例**后 swap 降到 7.9GB、可用 54%，RTF 就稳定在 0.6x 了。

⚠️ 绝不要拿 `INDEXTTS_TIMEOUT`(300s) 去死等单请求；单请求用
`INDEXTTS_CHUNK_TIMEOUT`（默认 90s），超时立刻切 `INDEXTTS_FAILOVER_URLS` 的下一个实例。

⚠️ **别用"测试调用按钮"能否通过来判断长稿能否合成**：该端点只发
「TTS 烟雾测试。」7 个字，几秒就返回，跟 3000+ 字成稿完全是两回事。
"""

import asyncio
import base64
import io
import logging
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import TTSClient

log = logging.getLogger(__name__)


class IndexTTSError(RuntimeError):
    """IndexTTS 合成失败（网络 / 服务 4xx-5xx / 空音频）。"""


# 长稿切块阈值（字）。
#
# 实测（:8000 / IndexTTS-1.5，真实 LLM 成稿，非重复句）——**关键发现是这个端点会截断**：
#
#     150 字 → 音频 17.9s  语速 8.4 字/秒   ✅ 正常
#     300 字 → 音频  6.9s  语速 43.7 字/秒  ❌ 截断
#     600 字 → 音频 19.8s  语速 30.4 字/秒  ❌ 截断
#
# 语速超过 ~10 字/秒就说明音频被提前截断了（正常中文播报 4~6 字/秒）。
# 所以单块必须控制在 ~150 字以内，否则会**静默丢内容**——请求成功、音频却只有开头一小段。
# 150 字实测耗时约 30s，相对 90s 的单请求超时还有约 3 倍余量。
_CHUNK_CHARS = int(os.getenv("INDEXTTS_CHUNK_CHARS", "150"))
# 句读边界（中英文标点 + 换行），优先在这些位置切，避免把一句话劈两半。
_SENTENCE_END = "。！？；\n.!?;"


def _sibling_omlx_urls(base_url: str, ports: tuple[int, ...] = (8000, 8008)) -> list[str]:
    """列出本机所有 oMLX 候选地址（base_url 排最前，其余按端口序）。

    本机常同时跑着 launchd 托管的 `:8000` 和 oMLX GUI 拉起的 `:8008`，
    两者都会间歇空转且交替发生 —— 所以把它们串成候选链做故障切换。
    非本机 base_url（如远程 oMLX）不猜端口，只返回它自己。
    """
    host = urlparse(base_url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        return [base_url]
    ordered = [base_url] + [
        f"http://127.0.0.1:{p}/v1" for p in ports if f"http://127.0.0.1:{p}/v1" != base_url
    ]
    return list(dict.fromkeys(ordered))


def _wav_duration_sec(data: bytes) -> float | None:
    """读 WAV 时长（秒）；不是合法 WAV 返回 None。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            if not rate:
                return None
            return w.getnframes() / float(rate)
    except Exception:
        return None


# 正常中文播报约 4~6 字/秒。留足余量后仍高于此值 → 音频被截断。
# 实测截断样本：300 字只产出 6.9s（43.7 字/秒）、600 字产出 19.8s（30.4 字/秒）。
_TRUNCATION_CHARS_PER_SEC = 12.0


def _assert_not_truncated(text: str, audio: bytes) -> None:
    """校验音频长度与文本匹配，防止"HTTP 200 但内容被静默截断"。

    这个端点在输入超长时会返回 200 + 合法 WAV，但只保留开头一小段。
    不校验的话，下游会把缺内容的音频当成功写库，线上极难发现。
    """
    dur = _wav_duration_sec(audio)
    if dur is None or dur <= 0:
        log.warning("IndexTTS 音频时长不可解析，跳过截断校验")
        return
    cps = len(text) / dur
    if cps > _TRUNCATION_CHARS_PER_SEC:
        raise IndexTTSError(
            f"IndexTTS 输出疑似被截断：{len(text)} 字只产出 {dur:.1f}s 音频"
            f"（{cps:.1f} 字/秒，正常应 < {_TRUNCATION_CHARS_PER_SEC}）。"
            f"请调小 INDEXTTS_CHUNK_CHARS（当前 {len(text) and _CHUNK_CHARS}）。"
        )


def _split_into_chunks(text: str, limit: int) -> list[str]:
    """按句读边界把长文本切成**严格不超过 limit 字**的块（CP-INDEXTTS-CHUNK）。

    - 先按句读切句，再贪心装箱；保证每块 ≤ limit（这点很关键：块一旦超过
      ~150 字，端点会静默截断，音频只保留开头）。
    - 单句本身超长时按硬切兜底（不会丢字）。
    """
    if limit <= 0 or len(text) <= limit:
        return [text]

    # 1) 切成句子（保留句末标点）
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_END:
            sentences.append(buf)
            buf = ""
    if buf:
        sentences.append(buf)

    # 2) 贪心装箱，严格不超 limit
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        if len(sent) > limit:
            # 单句超长：先把当前块收尾，再硬切这句
            if cur.strip():
                chunks.append(cur.strip())
                cur = ""
            rest = sent
            while len(rest) > limit:
                chunks.append(rest[:limit])
                rest = rest[limit:]
            cur = rest
            continue
        if len(cur) + len(sent) <= limit:
            cur += sent
        else:
            if cur.strip():
                chunks.append(cur.strip())
            cur = sent
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _concat_wav(pieces: list[bytes]) -> bytes:
    """把多段 WAV 字节拼成一段（CP-INDEXTTS-CHUNK）。

    优先用 ffmpeg 的 concat demuxer（`-c copy`，不重编码、几乎零开销、
    自动重算 WAV 头）；ffmpeg 不可用时退化为「取首段 WAV 头 + 顺次拼 PCM 载荷」。
    退化路径对采样率/位宽/声道完全一致的 IndexTTS 输出是安全的。
    """
    if not pieces:
        raise IndexTTSError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]

    ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
    with tempfile.TemporaryDirectory(prefix="indextts_concat_") as tmp:
        listing = os.path.join(tmp, "list.txt")
        lines = []
        for i, piece in enumerate(pieces):
            p = os.path.join(tmp, f"part_{i:04d}.wav")
            Path(p).write_bytes(piece)
            lines.append(f"file '{p}'")
        Path(listing).write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = os.path.join(tmp, "merged.wav")
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    listing,
                    "-c",
                    "copy",
                    out,
                ],
                capture_output=True,
                timeout=120,
                check=True,
            )
            merged = Path(out).read_bytes()
            if len(merged) > 100:
                return merged
            log.warning("IndexTTS ffmpeg 拼接产出异常(%dB)，退回字节拼接", len(merged))
        except FileNotFoundError:
            log.warning("IndexTTS 未找到 ffmpeg(%s)，退回字节拼接", ffmpeg)
        except subprocess.SubprocessError as exc:
            log.warning("IndexTTS ffmpeg 拼接失败(%s)，退回字节拼接", exc)

    # 退化：首段留 WAV 头，其余段只取 data chunk（跳过 44 字节 RIFF 头）
    head = pieces[0]
    data = bytearray()
    for i, piece in enumerate(pieces):
        if i == 0:
            data += piece
            continue
        idx = piece.find(b"data")
        if idx == -1:
            data += piece
        else:
            # "data" + 4 字节 size + 载荷
            data += piece[idx + 8 :]
    _patch_riff_sizes(head, len(data))
    return bytes(data)


def _patch_riff_sizes(wav_header: bytes, total_len: int) -> bytes:
    """把 RIFF/data 的大小字段改成拼接后的真实长度。"""
    buf = bytearray(wav_header)
    if len(buf) >= 4 and buf[:4] == b"RIFF":
        buf[4:8] = (total_len - 8).to_bytes(4, "little")
    idx = buf.find(b"data")
    if idx != -1 and idx + 8 <= len(buf):
        buf[idx + 4 : idx + 8] = (total_len - idx - 8).to_bytes(4, "little")
    return bytes(buf)


class IndexTTSClient(TTSClient):
    """oMLX 托管 IndexTTS-1.5（OpenAI 兼容 + ref_audio 零样本克隆）。"""

    DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
    """launchd 托管（KeepAlive）的 omlx-cli serve 端口 —— 选它是因为进程挂了会被
    自动拉起，不是为了绕开什么 8008 故障（两个实例都会间歇空转，见文件头说明）。"""

    CHUNK_CHARS = _CHUNK_CHARS
    """超过这个字数就分段合成（见文件头「合成耗时随字数暴涨」）。"""
    DEFAULT_MODEL = "IndexTTS-1.5"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        ref_audio_path: str | None = None,
        ref_text: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (base_url or os.getenv("INDEXTTS_BASE_URL", self.DEFAULT_BASE_URL)).rstrip(
            "/"
        )
        self.model = model or os.getenv("INDEXTTS_MODEL", self.DEFAULT_MODEL)
        self.ref_audio_path = ref_audio_path or os.getenv("INDEXTTS_REF_AUDIO", "")
        self.ref_text = ref_text or os.getenv("INDEXTTS_REF_TEXT", "")
        # 超时可配：oMLX 首次加载 IndexTTS 模型 / 机器负载高时，单段合成可能远超
        # 120s，硬编码会让蒸馏在 TTS 阶段必然超时失败（实测「合成超时(120.0s)」）。
        # 默认放宽到 300s，可用 INDEXTTS_TIMEOUT 覆盖。
        self.timeout = (
            timeout if timeout is not None else float(os.getenv("INDEXTTS_TIMEOUT", "300"))
        )
        # CP-INDEXTTS-FAILOVER：单请求超时 vs 整体超时分开。
        #
        # 实测 oMLX 实例会**间歇性空转**（50% CPU、RSS 不涨、合成端点零响应），
        # 13 字短文本也会无限期挂起；空转窗口过去后自己恢复（同一分钟内
        # 8000 挂 / 8008 正常，下一分钟又反过来）。所以：
        #   - 拿 300s 去等一个卡死的实例 = 白等 5 分钟，还必然失败；
        #   - 单请求用较短超时（默认 90s）快速失败，再切到另一个实例重试。
        # 两个实例交替空转 → 故障切换后成功率显著高于死等单一实例。
        self.chunk_timeout = float(os.getenv("INDEXTTS_CHUNK_TIMEOUT", "90"))
        failover = os.getenv("INDEXTTS_FAILOVER_URLS", "").strip()
        candidates = [u.strip().rstrip("/") for u in failover.split(",") if u.strip()]
        if not candidates:
            # 没显式配 failover 时，自动把本机另一个 oMLX 端口补进候选链 ——
            # 两个实例交替空转（见文件头），单靠死等主地址必然失败。
            candidates = _sibling_omlx_urls(self.base_url)
        else:
            candidates = [self.base_url] + candidates
        # 去重保序
        self.endpoints: list[str] = list(dict.fromkeys(candidates))
        self.max_attempts = max(1, int(os.getenv("INDEXTTS_MAX_ATTEMPTS", "2")))
        # 分段并发度。
        #
        # ⚠️ **默认 1（串行）**，这不是保守，是实测逼出来的：
        #   - 600 字 6 块**串行** → ✅ 成功
        #   - 4734 字 37 块**并发 3** → ❌ 2 实例 × 2 次尝试全部 90s 超时
        #
        # 并发会直接吃内存：实测并发 3 时 oMLX writable regions 从 4.9GB 涨到
        # 11.9GB、swapped_out 50%、整机 swap 从 7.9GB 回到 11.9GB —— 把刚治好的
        # 内存耗尽又请回来，推理退化成磁盘换页，请求就再也回不来了。
        #
        # oMLX 默认 max_concurrent_requests=8 是给内存充裕的大机器的；16GB 机器
        # 不要照抄。机器内存充裕时可调高（INDEXTTS_CONCURRENCY），但请先盯
        # `sysctl vm.swapusage` 和 oMLX 的 swapped_out 比例。
        self.chunk_concurrency = max(1, int(os.getenv("INDEXTTS_CONCURRENCY", "1")))
        # trust_env=False：忽略沙箱/系统 HTTP(S)_PROXY（代理漂移会打挂本地 127.0.0.1 请求）
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        # 参考音频 base64 缓存（文件不重新读盘，除非 ref_audio_path 变化）
        self._ref_b64: str | None = None
        self._ref_b64_for: str | None = None

    @property
    def provider_name(self) -> str:
        return "indextts"

    # -- 参考音频 ---------------------------------------------------------
    async def _load_ref_audio_b64(self, source: str | None = None) -> str:
        """读参考 wav → base64（缓存，来源变更才重读）。

        CP-TTS-VOICE：`source` 支持两种来源 ——
          - **本地绝对路径**（历史行为，配 INDEXTTS_REF_AUDIO）
          - **http(s) URL**（音色库存上传后的地址，指向 S3/OSS）

        音色库（`common/tts_voice_service.py`）里每条音色存的就是这两种之一，
        所以这里必须两种都认 —— 之前只认 `Path.is_file()`，音色库存 URL 会直接
        报「参考音频文件不存在」。

        缓存键是**来源字符串本身**，所以同一个 client 连着合成不同音色的稿件时
        会自动换缓存（正常流程下 distill 一次只用一个音色，属防御性设计）。
        """
        src = (source or self.ref_audio_path or "").strip()
        if not src:
            raise IndexTTSError(
                "IndexTTS 需要参考音频：配置 INDEXTTS_REF_AUDIO（管理后台 TTS 设置页 "
                "indextts_ref_audio）或选择音色（tts_voices.ref_audio_url）。"
            )
        if self._ref_b64 is not None and self._ref_b64_for == src:
            return self._ref_b64

        if src.startswith(("http://", "https://")):
            data = await self._fetch_ref_audio(src)
        else:
            p = Path(src).expanduser()
            if not p.is_file():
                raise IndexTTSError(f"参考音频文件不存在: {p}")
            data = p.read_bytes()

        if len(data) < 1000:
            raise IndexTTSError(f"参考音频太小({len(data)}B)，可能不是有效 wav: {src}")
        self._ref_b64 = base64.b64encode(data).decode("ascii")
        self._ref_b64_for = src
        log.info("IndexTTS ref audio loaded: %s (%dB)", src, len(data))
        return self._ref_b64

    async def _fetch_ref_audio(self, url: str) -> bytes:
        """从 URL 拉参考音频。

        复用同一个 httpx client（已带 `trust_env=False`）。超时给 30s：
        音色库里的音频在 S3 上正常 <1s，但外网 OSS 可能慢。
        """
        try:
            resp = await self._client.get(url, timeout=30.0)
        except httpx.HTTPError as exc:
            raise IndexTTSError(f"参考音频下载失败: {url} ({exc})") from exc
        if resp.status_code != 200:
            raise IndexTTSError(f"参考音频下载失败 HTTP {resp.status_code}: {url}")
        return resp.content

    def _ensure_ref_text(self, override: str | None = None) -> str:
        """取参考文本。`override` 非 None 时用它（音色库自带 ref_text）。"""
        text = override if override is not None else self.ref_text
        if not (text or "").strip():
            raise IndexTTSError(
                "IndexTTS 需要参考音频转录文本：配置 INDEXTTS_REF_TEXT"
                "（管理后台 TTS 设置页 indextts_ref_text，须与 ref_audio 内容一致）。"
            )
        return text

    # -- 合成 -------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        voice: str | None = None,  # 契约兼容；IndexTTS 音色由 ref_audio 决定
        output_format: str = "wav",
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ) -> bytes:
        """合成文本为音频字节。

        CP-INDEXTTS-CHUNK：长稿分段合成。
        合成耗时随字数近似线性上涨（实测 600 字 ≈ 50s），蒸馏成稿普遍 3000~4000
        字，单请求必然撞 `INDEXTTS_TIMEOUT`（实测「合成超时(300.0s)」）。
        这里在句读边界切块 → 逐块合成 → 拼接为单个 WAV 返回，
        使**单次请求**始终远小于超时阈值（单块 ≤ CHUNK_CHARS 字）。

        CP-TTS-VOICE：`ref_audio` / `ref_text` 允许**按调用覆盖**音色。
        传 None 时用 client 自身的全局配置（历史行为不变）。
        蒸馏时由 `resolve_voice_for_user()` 解析出用户选的那个音色传进来 ——
        音色是 IndexTTS 的「参考音频 + 参考文本」对，不是能填名字的参数。
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        body = text.strip()
        if len(body) <= self.CHUNK_CHARS:
            return await self._synthesize_once(body, ref_audio, ref_text)

        chunks = _split_into_chunks(body, self.CHUNK_CHARS)
        log.info(
            "IndexTTS 分段合成: total_chars=%d chunks=%d chunk_chars<=%d concurrency=%d",
            len(body),
            len(chunks),
            self.CHUNK_CHARS,
            self.chunk_concurrency,
        )
        sem = asyncio.Semaphore(self.chunk_concurrency)
        done = 0

        async def run(i_chunk: tuple[int, str]) -> bytes:
            nonlocal done
            i, chunk = i_chunk
            async with sem:
                piece = await self._synthesize_once(chunk, ref_audio, ref_text)
                done += 1
                log.info(
                    "IndexTTS 分段进度: %d/%d chars=%d bytes=%d",
                    done,
                    len(chunks),
                    len(chunk),
                    len(piece),
                )
                return piece

        # 分段之间彼此独立，按入队顺序返回，拼接后仍与原文一一对应。
        pieces = list(await asyncio.gather(*(run(p) for p in enumerate(chunks, 1))))
        merged = _concat_wav(pieces)
        _assert_not_truncated(body, merged)
        log.info("IndexTTS 分段合成完成: %d 段 -> %d bytes", len(pieces), len(merged))
        return merged

    async def _synthesize_once(
        self,
        text: str,
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ) -> bytes:
        """单次合成（不分段），带短超时 + 故障切换 + 重试。

        CP-INDEXTTS-FAILOVER：对每个 endpoint 轮转尝试，单请求超时用
        `chunk_timeout`（默认 90s），超时/连接错误立刻换下一个实例，
        而不是拿 300s 死等一个正在空转的实例。

        CP-TTS-VOICE：ref_audio / ref_text 为按调用覆盖的音色（见 synthesize）。
        """
        ref_b64 = await self._load_ref_audio_b64(ref_audio)
        ref_text_value = self._ensure_ref_text(ref_text)

        payload = {
            "model": self.model,
            "input": text.strip(),
            "ref_audio": ref_b64,
            "ref_text": ref_text_value,
        }
        log.info(
            "IndexTTS synthesize: model=%s text_chars=%d ref=%s endpoints=%s",
            self.model,
            len(text),
            ref_audio or self.ref_audio_path,
            self.endpoints,
        )

        last_err: Exception | None = None
        attempts = self.endpoints * self.max_attempts
        for i, base in enumerate(attempts):
            url = f"{base}/audio/speech"
            try:
                resp = await self._client.post(url, json=payload, timeout=self.chunk_timeout)
            except httpx.TimeoutException as e:
                last_err = e
                log.warning(
                    "IndexTTS 超时，切换实例: base=%s timeout=%ss attempt=%d/%d",
                    base,
                    self.chunk_timeout,
                    i + 1,
                    len(attempts),
                )
                continue
            except httpx.HTTPError as e:
                last_err = e
                log.warning(
                    "IndexTTS HTTP 异常，切换实例: base=%s err=%s attempt=%d/%d",
                    base,
                    e,
                    i + 1,
                    len(attempts),
                )
                continue

            if resp.status_code >= 400:
                last_err = IndexTTSError(f"IndexTTS HTTP {resp.status_code}: {resp.text[:400]}")
                log.warning(
                    "IndexTTS 业务错误 %s: base=%s body=%s",
                    resp.status_code,
                    base,
                    resp.text[:200],
                )
                continue

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
                last_err = IndexTTSError(
                    f"IndexTTS 返回空音频（status={resp.status_code}, ct={ct}）"
                )
                log.warning("IndexTTS 空音频，切换实例: base=%s ct=%s", base, ct)
                continue
            log.info("IndexTTS synthesized: %d bytes (%s) via %s", len(audio), ct, base)
            return audio

        raise IndexTTSError(
            f"IndexTTS 合成失败（已尝试 {len(attempts)} 次 / "
            f"{len(self.endpoints)} 个实例，请求超时 {self.chunk_timeout}s）：{last_err}"
        ) from last_err

    async def close(self) -> None:
        await self._client.aclose()
