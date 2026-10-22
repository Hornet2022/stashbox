"""TTS 长文本切块与音频拼接（IndexTTS 与 OpenAI 协议 provider 共用）。

## 为什么抽成共享模块

两个 provider 都撞过「长稿合成不出来」：

- **IndexTTS**：端点会**静默截断**长输入（返回 HTTP 200 + 合法 WAV，但音频只剩
  开头一小段），所以必须切块，见 indextts.py 顶部的实测记录。
- **OpenAI 协议**（生产在用：tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）：
  端点不截断，但**整篇合成会超时**。2026-10-21 实测：
      1503B / ~500 字 → 40s 成功
      1636B / ~545 字 → 41s 成功
      3142B / ~1047 字 → 60s **超时失败**（httpx 固定 60s 墙钟）
  合成耗时随字数线性增长，固定超时对长稿必然击穿。

两者解法是同一个：**按句读边界切块 → 逐块合成 → 拼接**。
放在一处，避免第二个 provider 再踩一次「只给 IndexTTS 分段」的坑
（agent 路径当时就只给 IndexTTS 分段，OpenAI provider 整篇合成 → 长稿必超时）。

## 拼接的格式差异

- **MP3**：每帧自包含，**直接字节拼接**即可，播放器按帧顺序解码。
  生产的 OpenAI provider 返回的就是 mp3（`response_format=mp3`）。
- **WAV**：不能直接拼（RIFF 头里的长度字段只描述第一段）。用 ffmpeg concat
  demuxer（`-c copy`，不重编码）；ffmpeg 不可用时退化为「首段 WAV 头 +
  顺次拼 PCM 载荷」并修正 RIFF/data 长度字段。
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_SENTENCE_END = "。！？；\n.!?;"


def split_into_chunks(text: str, limit: int) -> list[str]:
    """按句读边界把长文本切成**严格不超过 limit 字**的块。

    - 先按句读切句，再贪心装箱；保证每块 ≤ limit（IndexTTS 端点一旦超过
      ~150 字就会静默截断，音频只保留开头）。
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


def patch_riff_sizes(wav_header: bytes, total_len: int) -> bytes:
    """把 RIFF/data 的大小字段改成拼接后的真实长度。"""
    buf = bytearray(wav_header)
    if len(buf) >= 4 and buf[:4] == b"RIFF":
        buf[4:8] = (total_len - 8).to_bytes(4, "little")
    idx = buf.find(b"data")
    if idx != -1 and idx + 8 <= len(buf):
        buf[idx + 4 : idx + 8] = (total_len - idx - 8).to_bytes(4, "little")
    return bytes(buf)


def concat_wav(pieces: list[bytes]) -> bytes:
    """把多段 WAV 字节拼成一段。

    优先用 ffmpeg 的 concat demuxer（`-c copy`，不重编码、几乎零开销、
    自动重算 WAV 头）；ffmpeg 不可用时退化为「取首段 WAV 头 + 顺次拼 PCM 载荷」。
    退化路径对采样率/位宽/声道完全一致的输出是安全的。
    """
    if not pieces:
        raise ValueError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]

    merged = _concat_via_ffmpeg(pieces, "wav")
    if merged is not None:
        return merged

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
    return patch_riff_sizes(head, len(data))


def _concat_via_ffmpeg(pieces: list[bytes], ext: str) -> bytes | None:
    """用 ffmpeg concat demuxer 拼接同格式音频片段（`-c copy`，不重编码）。

    返回合并后的字节；ffmpeg 不可用或失败时返回 None（调用方决定退化路径）。

    ⚠️ 为什么 MP3 也必须走 ffmpeg，而不是直接字节拼接：
    裸拼出来的 MP3 没有统一的帧头索引，ffmpeg 会报

        invalid concatenated file detected - using bitrate for duration
        Estimating duration from bitrate, this may be inaccurate

    —— 时长变成**估算值**而不是解析值。对一个播客产品来说这是硬伤：
    客户端展示的时长、进度条、剩余时间都会不准（实测分段拼接的两篇
    都触发了这个警告，而未分段的单文件没有）。

    concat demuxer 会重新扫描全部帧并写出正确的时长表，`-c copy` 不重编码，
    耗时与文件大小同阶、几乎零 CPU。
    """
    ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
    with tempfile.TemporaryDirectory(prefix="tts_concat_") as tmp:
        listing = os.path.join(tmp, "list.txt")
        lines = []
        for i, piece in enumerate(pieces):
            p = os.path.join(tmp, f"part_{i:04d}.{ext}")
            Path(p).write_bytes(piece)
            lines.append(f"file '{p}'")
        Path(listing).write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = os.path.join(tmp, f"merged.{ext}")
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
            log.warning("TTS ffmpeg 拼接产出异常(%dB)，退回字节拼接", len(merged))
        except FileNotFoundError:
            log.warning("TTS 未找到 ffmpeg(%s)，退回字节拼接", ffmpeg)
        except subprocess.SubprocessError as exc:
            log.warning("TTS ffmpeg 拼接失败(%s)，退回字节拼接", exc)
    return None


def concat_mp3(pieces: list[bytes], gap_sec: float = 0.3) -> bytes:
    """把多段 MP3 字节拼成一段。

    顺序很重要：
      ① **响度归一**（每段）→ 消除段间音量跳变
      ② **交叉淡化**（段间 300ms）→ 消除硬切，让音色过渡自然
      ③ 兜底：插入段间静音 + ffmpeg concat demuxer
      ④ 最后退化：裸字节拼接（不丢内容）

    ## 响度归一为什么必须逐段做

    实测（2026-10-22，同一段文字合成两次）：-26.0 / -22.5 LUFS ——
    **同一句话都能差 3.9 LU**。分段后段间差 2.9 LU。
    整体归一不会改变段与段的相对差异，必须逐段做。
    音量跳变本身就会被听成「换人了」，与音色差异叠加更碎。

    ## 为什么不插段间静音当主路径

    原来每段之间插 0.3s 静音（让听感像换气）。实测那**没用** ——
    内测反馈的「一篇三种音色」就是带着静音拼出来的音频。
    静音只让耳朵「重新分句」，两段依然是两个不同的渲染；
    交叉淡化才真的把接缝**抹掉**。
    静音降级成 ③ 的兜底（ffmpeg 不可用时还能用上）。

    ## 关于音高对齐（默认关闭）

    曾试过按基频把各段拉平（voice_align.align_pitch）。**结论是不该做**，
    理由是度量本身不可信：

    实测同一篇稿子 3 段的基频分布（YIN，逐帧）：

        段          p10   p25   中位   p90
        seg1 run1    92   103   121   247
        seg1 run2   100   118   167   434   ← 同一段，再合成一次
        seg2 run1   103   123   178   405
        seg3 run1   100   127   183   396

    稳健统计量（p10 / p25）跨所有段、跨两次重跑都几乎一致，
    长时平均谱（LTAS）段间差也只有 0.11~0.31dB ——
    **各段的音色本来就是同一个人**。只有中位数在乱跳
    （同一段两次 121 vs 167），因为真实语音的 F0 分布很宽
    （94~570Hz），中位数被随机采样的上尾带跑了。

    也就是说：段间并没有系统性的音高差，**「三种音色」的成因不是音高**。
    强行拉平只会毁掉自然的语调起伏，而且「该往哪个方向拉」判不准 ——
    实测对 118Hz 的段施加 ×1.587 后，测得中位变成 304Hz（预期 ~188Hz）。

    所以音高对齐保留为**可开关的实验**（OPENAI_TTS_ALIGN_PITCH=1），
    默认关闭；要重新启用前得先有一个在真实语音上可信的基频度量。
    """
    if not pieces:
        raise ValueError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]

    # ① 音高对齐（默认关闭 —— 见上面「关于音高对齐」的实测结论）
    if os.getenv("OPENAI_TTS_ALIGN_PITCH", "").strip() in ("1", "true", "yes"):
        try:
            from .voice_align import align_pitch

            aligned, details = align_pitch(pieces)
            if any(d.get("semitones") for d in details):
                log.info("TTS 音高对齐: %s", details)
            pieces = aligned
        except Exception as exc:  # noqa: BLE001 - 对齐失败不能影响主流程
            log.warning("TTS 音高对齐异常，保留原音频: %s", exc)

    # ② 响度归一（逐段）
    normalized: list[bytes] = []
    for piece in pieces:
        norm = normalize_loudness(piece)
        normalized.append(norm if norm else piece)
    pieces = normalized

    # ③ 交叉淡化（失败则退回）
    faded = crossfade_concat(pieces)
    if faded:
        return faded

    # ④ 兜底：段间静音 + ffmpeg concat demuxer（重算时长表，避免时长变成估算值）
    with_gaps = _insert_gaps(pieces, gap_sec)
    merged = _concat_via_ffmpeg(with_gaps, "mp3")
    if merged is not None:
        return merged
    # 最后退化：MP3 每帧自包含，直接字节拼接播放器也能按帧顺序解码
    return b"".join(with_gaps)


def _insert_gaps(pieces: list[bytes], gap_sec: float) -> list[bytes]:
    """在段间插入静音（crossfade 不可用时的兜底）。"""
    if gap_sec <= 0 or len(pieces) < 2:
        return list(pieces)

    from .audio_duration import mp3_info

    info = mp3_info(pieces[0])
    if not info:
        return list(pieces)
    _, rate, channels = info
    silence = silence_mp3(gap_sec, rate, channels)
    if not silence:
        return list(pieces)

    out: list[bytes] = []
    for i, piece in enumerate(pieces):
        if i:
            out.append(silence)
        out.append(piece)
    return out


def silence_mp3(
    duration_sec: float,
    sample_rate: int = 24000,
    channels: int = 1,
    ffmpeg_bin: str | None = None,
) -> bytes | None:
    """生成一段静音 mp3（参数需与内容一致，才能 `-c copy` 拼接）。

    ## 为什么要段间静音

    分段合成是**多次独立请求** —— 每段的韵律/语调各自采样，段与段之间
    会有突变，听感上「一段一段」（内测反馈：音色感觉是一段一段的）。

    在段与段之间插一小段静音，人耳会把每段当成独立的句子/换气，
    而不是「一段连续语音被切开」。旧 pipeline（distill/steps.py 的
    `gap_after_ms=350`）就是这么做的。

    静音的**采样率与声道数必须和内容一致**，否则 ffmpeg concat demuxer
    会重采样 → `-c copy` 不成立（要么报错，要么悄悄重编码）。
    调用方应从内容里探测（audio_duration.mp3_info）后传进来。

    ffmpeg 不可用时返回 None —— 调用方跳过插入，不破主流程。
    """
    if duration_sec <= 0:
        return None
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="tts_silence_") as tmp:
            out = os.path.join(tmp, "silence.mp3")
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={sample_rate}:cl={'mono' if channels == 1 else 'stereo'}",
                    "-t",
                    f"{duration_sec:.3f}",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "9",
                    out,
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            data = Path(out).read_bytes()
            return data if len(data) > 100 else None
    except FileNotFoundError:
        log.warning("TTS 生成段间静音失败（无 ffmpeg）")
    except subprocess.SubprocessError as exc:
        log.warning("TTS 生成段间静音失败：%s", exc)
    return None


def crossfade_concat(
    pieces: list[bytes],
    duration: float = 0.3,
    ffmpeg_bin: str | None = None,
    bitrate: str = "64k",
) -> bytes | None:
    """用 ffmpeg acrossfade 把多段**交叉淡化**拼接（消除段间硬切）。

    ## 为什么需要

    分段合成 = 多次独立请求。Qwen3-TTS 是 zero-shot 克隆，**每次请求都从
    参考音频重新采样韵律** → 段与段的音色/语气有差异。内测反馈：
    「一篇音频大概能明显听出来有三种音色」——实测就是 3 段（稿子 ~1057 字
    ÷ 500 字/段）。

    一次合成能彻底消除，但上游有 60s 墙钟（504 backend_timeout），
    实测 1000 字必然撞线 —— 所以「整篇一次合成」在上游限制下做不到。

    交叉淡化让 A 的尾 300ms 与 B 的头 300ms 重叠混合：听感上是「一个人的
    音色在变」，而不是「突然换人念」。

    ## 代价

    - 需要**重编码**（acrossfade 是 filter，不能 `-c copy`）
    - 总时长会缩短 (段数-1) × duration（重叠部分被混合掉）——
      `duration_sec` 是从文件实测的，所以仍然准确
    - ffmpeg 不可用 / 失败 → 返回 None，调用方退回直接拼接

    ## 另一个方向（更彻底，但受限）

    段长放大 → 段数变少 → 接缝变少。实测上游 60s 墙钟下 500 字 wall 35.7s、
    1000 字必 504，所以段长的实际上限在 500~600 字。
    """
    if len(pieces) < 2 or duration <= 0:
        return None
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="tts_xfade_") as tmp:
            inputs: list[str] = []
            for i, piece in enumerate(pieces):
                p = os.path.join(tmp, f"part_{i:04d}.mp3")
                Path(p).write_bytes(piece)
                inputs.append(p)

            # 链式 acrossfade：[0][1]xf→x；[x][2]xf→x2 …
            chains = []
            prev = "0:a"
            for i in range(1, len(pieces)):
                out_label = f"x{i}"
                chains.append(f"[{prev}][{i}:a]acrossfade=d={duration}:c1=tri:c2=tri[{out_label}]")
                prev = out_label
            out = os.path.join(tmp, "merged.mp3")
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            for p in inputs:
                cmd += ["-i", p]
            cmd += [
                "-filter_complex",
                ";".join(chains),
                "-map",
                f"[{prev}]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                out,
            ]
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
            data = Path(out).read_bytes()
            if len(data) > 100:
                return data
            log.warning("TTS crossfade 产出异常(%dB)，退回直接拼接", len(data))
    except FileNotFoundError:
        log.warning("TTS crossfade 失败（无 ffmpeg），退回直接拼接")
    except subprocess.SubprocessError as exc:
        log.warning("TTS crossfade 失败：%s，退回直接拼接", exc)
    return None


def normalize_loudness(
    piece: bytes,
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
    ffmpeg_bin: str | None = None,
) -> bytes | None:
    """把一段音频的响度归一到目标（EBU R128，ffmpeg loudnorm）。

    ## 为什么需要

    分段合成 = 多次独立请求，zero-shot 克隆每次的**响度**也会不同
    （内容长短、重音分布都会影响输出电平）。段间音量跳变同样会被听成
    「换人了」—— 与音色差异叠加，听感更碎。

    统一响度后，段与段之间只剩音色差异，突兀感明显降低。

    ## 代价

    - 每段一次 ffmpeg 调用（CPU，单遍动态模式，实测 20 秒音频 < 1 秒）
    - loudnorm 的动态模式会做动态压缩（不是纯粹改增益），动态范围略收窄
    - ffmpeg 不可用 / 失败 → 返回 None，调用方退回原音频（宁可不统一，
      也不能丢内容）
    """
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="tts_loud_") as tmp:
            src = os.path.join(tmp, "in.mp3")
            out = os.path.join(tmp, "out.mp3")
            Path(src).write_bytes(piece)
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    src,
                    "-af",
                    f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    out,
                ],
                capture_output=True,
                timeout=120,
                check=True,
            )
            data = Path(out).read_bytes()
            if len(data) > 100:
                return data
            log.warning("TTS 响度归一产出异常(%dB)，退回原音频", len(data))
    except FileNotFoundError:
        log.warning("TTS 响度归一失败（无 ffmpeg），退回原音频")
    except subprocess.SubprocessError as exc:
        log.warning("TTS 响度归一失败：%s，退回原音频", exc)
    return None


def concat_audio(pieces: list[bytes], gap_sec: float = 0.3) -> bytes:
    """按音频格式分派拼接：WAV / MP3 各走自己的路径。

    `gap_sec` 只在 MP3 路径上有意义：它是 crossfade 不可用时的兜底
    （段间静音）。正常路径走 crossfade，不需要额外静音。
    """
    if not pieces:
        raise ValueError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]
    if pieces[0][:4] == b"RIFF":
        return concat_wav(pieces)
    return concat_mp3(pieces, gap_sec=gap_sec)
