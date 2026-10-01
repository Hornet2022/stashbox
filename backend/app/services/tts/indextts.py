"""IndexTTS provider（OpenAI 兼容 /v1/audio/speech + 零样本音色克隆）。

服务：macmini 上自建的独立 mlx-audio 服务（:8010，launchd `com.stashbox.tts-serve`）。
**不再走 oMLX** —— oMLX 的 TTS 引擎内部本来就是 mlx-audio（它 `omlx/engine/tts.py`
里直接 `from mlx_audio.tts.utils import load_model`），但那 455 行封在 1.6GB 的
签名 app 里，它自己踩过的坑改不了只能等发版。拆出来自己接管之后，下面记录的
这类修复就成了本仓库里的普通代码。

端点契约（2026-09-22 建立，2026-10-01 在 :8010 上复验）：

    POST {base_url}/audio/speech
    {
      "model": "Qwen3-TTS-12Hz-0.6B-Base-bf16",
      "input": "待合成文本",
      "ref_audio": "<参考音频 wav 的 base64>",
      "ref_text": "参考音频的文字转录"
    }
    → 200 + audio/wav bytes

缺 ref_audio/ref_text 服务返回 400（"ref_text is required when ref_audio is provided"），
所以两者必须成对配置，缺一不可。

与 doubao/openai provider 的区别：
- voice 参数无意义 —— 音色由参考音频决定（克隆），保留形参只为契约兼容
- 返回是 WAV（RIFF），交给 step4/pipeline 落 .wav/.m4a 均能被 ExoPlayer 播

⚠️ **这个端点会静默截断长输入 —— 这是最关键的坑**（2026-10-01 在 :8010 复测）：

    122 字 → 语速 4.41 字/秒  ✅
    400 字 → 语速 4.63 字/秒  ✅ 安全上限
    800 字 → 语速 8.35 字/秒  ❌ 截断，音频比外推值短约 45%

请求会返回 **HTTP 200 + 合法 WAV**，但音频只有开头一小段，后面的内容凭空消失。
判定方法：语速超过 7 字/秒就是被截断了（正常中文播报 4~6 字/秒）。

所以 `synthesize()` 必须在 ~150 字处切块（`INDEXTTS_CHUNK_CHARS`），否则长稿会**静默丢内容**。

⚠️ **合成实时率约 2.8x**（别算错基准 —— 早期这里写的 0.6x 已无法复现）：

    38 字 → 23.5~24.2s 墙钟，产出 8.4~8.5s 音频，RTF 2.80~2.85x

即"生成 1 秒音频需要 2.8 秒计算"。用 oMLX 还是自建服务**完全一样**
（同一份权重、同样 GPU 访存、同样 16GB 内存墙），实测两者在噪声范围内。
并行也救不了：受控重测下并发 2 / 3 路各要 49.3s / 66.0s（≈ N × 单发），
推理被**完全串行化**，并发零吞吐增益，单流已打满 GPU 访存。
（早期「并发 2 会崩溃」是误判 —— 当时 8008 还挂着第二个 oMLX 实例，
 元凶是那个多出来的实例，不是并发本身。）

⚠️ **「合成端点零响应 + 50% CPU 空转」的真正根因是 mx.compile 冻结 RNG**
（不要再归因成「多实例打爆内存」，那条判断是错的）：

`mlx_lm/sample_utils` 把 `categorical_sampling` / `apply_top_k` /
`apply_top_p` / `apply_min_p` 都装饰了
`@partial(mx.compile, inputs=mx.random.state, outputs=mx.random.state)`。
这个装饰器在第一次调用后**不再推进全局 RNG 状态**，于是 TTS sampler 重放同一个
冻结的随机数；当冻结值偏向 codec EOS 列时，talker 在 step 0 就发 EOS，此后每次
请求都返回**零音频**。RNG 状态和 compile 缓存是进程级的、跨模型重载存活的，
所以**只有重启进程才能恢复** —— 症状正是「13 字短文本也无限期挂起」。

自建服务里已经打了这个补丁，见 `infra/mlx-audio-tts-service/tts_serve.py`。

⚠️ 绝不要拿 `INDEXTTS_TIMEOUT`(300s) 去死等单请求；单请求用
`INDEXTTS_CHUNK_TIMEOUT`（默认 90s），超时立刻切 `INDEXTTS_FAILOVER_URLS` 的下一个端点。

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
import time
import wave
from pathlib import Path

import httpx

from .base import TTSClient

log = logging.getLogger(__name__)


class IndexTTSError(RuntimeError):
    """IndexTTS 合成失败（网络 / 服务 4xx-5xx / 空音频）。"""


# 长稿切块阈值（字）。
#
# ⚠️ **这个端点会静默截断长输入** —— 请求返回 HTTP 200 + 合法 WAV，但音频只有
# 开头一小段，后面内容凭空消失。判定只能靠语速：正常中文播报 4~6 字/秒，
# 明显偏高就是被截断了。
#
# 2026-10-01 在自建服务（:8010 / Qwen3-TTS-12Hz-0.6B-Base-bf16）上重新实测，
# 真实 LLM 成稿、同一份参考音频：
#
#      38 字 → 墙钟  23.8s，音频  8.4s，RTF 2.83x， 4.52 字/秒  ✅
#     122 字 → 墙钟  80.4s，音频 27.7s，RTF 2.90x， 4.41 字/秒  ✅
#     400 字 → 墙钟 230.9s，音频 86.3s，RTF 2.68x， 4.63 字/秒  ✅ 安全上限
#     800 字 → 墙钟 248.2s，音频 95.8s，RTF 2.59x， 8.35 字/秒  ❌ 截断
#
# 800 字那行是**真截断**：按 400 字的 4.63 字/秒外推应产出约 173s 音频，
# 实际只有 95.8s —— 丢了约 45% 的内容，而 HTTP 状态是 200。
#
# 所以单块必须 ≤ 400 字。早年「150 字正常 / 300 字截断」那组数据是在 oMLX 上
# 测的，结论对当前服务已不适用（多半是当时 CHUNK_CHARS=400 配 90s 超时先超时、
# 拿到了 failover 的残缺响应，不是端点行为）。
#
# 默认仍取 200：实测 400 字是 0.577 s/字，比 130 字/段时的 0.7 s/字快约 18%，
# 但 400 字的墙钟 230.9s 已经贴着 INDEXTTS_CHUNK_TIMEOUT=240s 的上限，
# 放大的收益远小于超时风险。
_CHUNK_CHARS = int(os.getenv("INDEXTTS_CHUNK_CHARS", "200"))
# 句读边界（中英文标点 + 换行），优先在这些位置切，避免把一句话劈两半。
_SENTENCE_END = "。！？；\n.!?;"


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


# 截断判定的语速上限。
#
# ⚠️ 2026-10-01 收紧：原值 12.0 太宽松，**实测漏判过一次**。
# 正常样本语速 4.41~4.63 字/秒，800 字那个真截断样本是 8.35 字/秒 ——
# 12.0 的阈值对它判「正常」，于是约 45% 的内容静默丢失还能写进库。
#
# 取 7.0：离正常上界 4.63 还有 51% 余量，又稳稳低于真截断的 8.35。
# 宁可误报（合成失败重试）也不要漏报（内容缺失但状态成功）。
_TRUNCATION_CHARS_PER_SEC = 7.0


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
    """本地自建 mlx-audio TTS 服务（OpenAI 兼容 + ref_audio 零样本克隆）。"""

    DEFAULT_BASE_URL = "http://127.0.0.1:8010/v1"
    """自建服务的端口，launchd `com.stashbox.tts-serve` 托管（KeepAlive 只在非零
    退出时重启 + ThrottleInterval 30s），进程挂了会被自动拉起。"""

    CHUNK_CHARS = _CHUNK_CHARS
    """超过这个字数就分段合成（见 _CHUNK_CHARS 处的实测）。"""
    DEFAULT_MODEL = "Qwen3-TTS-12Hz-0.6B-Base-bf16"

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
        # 超时可配：首次加载 1.7GB 权重 / 机器负载高时，单段合成可能远超 120s，
        # 硬编码会让蒸馏在 TTS 阶段必然超时失败（实测「合成超时(120.0s)」）。
        # 默认放宽到 300s，可用 INDEXTTS_TIMEOUT 覆盖。
        self.timeout = (
            timeout if timeout is not None else float(os.getenv("INDEXTTS_TIMEOUT", "300"))
        )
        # CP-INDEXTTS-FAILOVER：单请求超时 vs 整体超时分开。
        #
        # 拿 300s 去等一个已经卡死/挂起的端点 = 白等 5 分钟，还必然失败；
        # 单请求用较短超时（默认 90s）快速失败，再切到候选链的下一个端点重试。
        self.chunk_timeout = float(os.getenv("INDEXTTS_CHUNK_TIMEOUT", "90"))
        # 候选链 = base_url + INDEXTTS_FAILOVER_URLS（逗号分隔，可为空）。
        #
        # 曾经这里会**无条件**把本机 8000/8008 两个 oMLX 端口猜进链里
        # （`_sibling_omlx_urls`），所以光删环境变量并不会变单端点，必须另设
        # `INDEXTTS_SINGLE_INSTANCE=1` 才能压住。oMLX 弃用后那套猜测已经没有
        # 对象，连同开关一起删掉：端点链现在完全由配置显式决定，行为可预测。
        failover = os.getenv("INDEXTTS_FAILOVER_URLS", "").strip()
        extra = [u.strip().rstrip("/") for u in failover.split(",") if u.strip()]
        # 去重保序
        self.endpoints: list[str] = list(dict.fromkeys([self.base_url, *extra]))
        self.max_attempts = max(1, int(os.getenv("INDEXTTS_MAX_ATTEMPTS", "2")))

        # CP-INDEXTTS-CIRCUIT：连续失败熔断。
        #
        # 为什么必须有：TTS 服务挂掉之后 HTTP 层仍然应答
        # （/v1/models 秒回 200、错 payload 返 422），只有真实合成请求会永久挂起。
        # 没有熔断的话，一段 200 字要走
        # 端点数 × max_attempts 次 × chunk_timeout 才报错 —— 实测 2 端点 × 2 次
        # × 240s = **单段最多烧 16 分钟**，一篇 8 段就是两个多小时，然后失败。
        # 熔断让「后端已经死了」变成几秒内的明确失败。
        self.breaker_threshold = max(1, int(os.getenv("INDEXTTS_BREAKER_THRESHOLD", "3")))
        self.breaker_cooldown = float(os.getenv("INDEXTTS_BREAKER_COOLDOWN", "300"))
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        # 分段并发度。
        #
        # ⚠️ **默认 1（串行）**，这不是保守，是实测逼出来的 —— 而且原因
        # 换过一次，早期那个「并发会吃爆内存」的归因是错的。
        #
        # 受控重测（机器空闲、无其它负载，同一段 38 字文本）：
        #     单发    24.2s → 8.5s 音频
        #     并发 2  各 49.3s → 合计 16.8s
        #     并发 3  各 66.0s → 合计 25.4s
        # 2/3 路**全部成功、进程存活、crash.log 无新记录**，所以并发本身不会
        # 搞崩服务（早期「并发 2 会崩」是误判：当时 8008 还挂着第二个 oMLX
        # 实例，元凶是那个多出来的实例）。
        # 但 49.3 ≈ 2×24.2、66.0 ≈ 3×22 → 推理被**完全串行化**，并发零吞吐
        # 增益，只是把每段的等待时间按倍数拉长。单流已打满 GPU 访存。
        # 所以 TTS 侧和 ARQ_MAX_JOBS 都应保持 1。
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

        CP-INDEXTTS-CIRCUIT：连续 [breaker_threshold] 段彻底失败后开闸
        [breaker_cooldown] 秒，期间直接快速失败，不再重试。
        """
        # 熔断检查放在最前面 —— 包括 ref 音频加载之前，这样连冷读 1.7GB 权重
        # 的开销都省掉。后端已死时重试到底只会把机器拖得更慢。
        now = time.monotonic()
        if self._circuit_open_until > now:
            raise IndexTTSError(
                f"IndexTTS 熔断中：已连续失败 {self._consecutive_failures} 段，"
                f"{self._circuit_open_until - now:.0f}s 后重试"
                f"（端点 {self.endpoints}）。先看 "
                f"launchctl list | grep tts-serve 和 /tmp/stashbox-tts-serve.err。"
            )

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
            # 本服务正常返回 audio/wav；若哪天返回 JSON base64 兜底解一下
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
            # 成功即复位熔断计数：一次成功说明后端活了。
            if self._consecutive_failures:
                log.info("IndexTTS 熔断计数复位（此前连续失败 %d 段）", self._consecutive_failures)
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            return audio

        # 全部尝试用尽 —— 计入熔断。
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker_threshold:
            self._circuit_open_until = time.monotonic() + self.breaker_cooldown
            log.error(
                "IndexTTS 熔断打开：连续 %d 段失败（阈值 %d），" "暂停 %ss 再试。端点=%s",
                self._consecutive_failures,
                self.breaker_threshold,
                self.breaker_cooldown,
                self.endpoints,
            )

        raise IndexTTSError(
            f"IndexTTS 合成失败（已尝试 {len(attempts)} 次 / "
            f"{len(self.endpoints)} 个实例，请求超时 {self.chunk_timeout}s，"
            f"连续失败第 {self._consecutive_failures} 段）：{last_err}"
        ) from last_err

    async def close(self) -> None:
        await self._client.aclose()
