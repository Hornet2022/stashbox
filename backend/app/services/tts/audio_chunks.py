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


def concat_mp3(pieces: list[bytes]) -> bytes:
    """把多段 MP3 字节拼成一段。

    优先走 ffmpeg concat demuxer（重算时长表，避免「invalid concatenated
    file detected」导致的时长估算不准）；ffmpeg 不可用时退化为字节拼接
    —— 播放器仍能按帧顺序播，但时长会变成估算值。
    """
    if not pieces:
        raise ValueError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]

    merged = _concat_via_ffmpeg(pieces, "mp3")
    if merged is not None:
        return merged
    # 退化：MP3 每帧自包含，直接字节拼接播放器也能按帧顺序解码
    return b"".join(pieces)


def concat_audio(pieces: list[bytes]) -> bytes:
    """按音频格式分派拼接：WAV / MP3 各走自己的路径。"""
    if not pieces:
        raise ValueError("没有可拼接的音频分段")
    if len(pieces) == 1:
        return pieces[0]
    if pieces[0][:4] == b"RIFF":
        return concat_wav(pieces)
    return concat_mp3(pieces)
