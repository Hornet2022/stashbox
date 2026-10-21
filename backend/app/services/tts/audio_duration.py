"""音频时长计算（WAV / MP3），无第三方依赖。

## 为什么不用「字节数 // 32000」

原来的实现是 `len(audio) // 32000` —— 假设音频是 32 kbps 的 CBR。
生产上的 TTS 走 OpenAI 协议（Qwen3-TTS-12Hz-1.7B-Base），实际码率是
**55 kbps** → 时长被低估 1.7 倍：

    真实 223.4s 的音频 → 记成 1540989 // 32000 = **48 秒**

这不是纯显示问题。`duration_sec` 在安卓端还用于：

- 详情页 / 列表页展示时长
- 「听感评分 ≥ 90% 触发」的判定（当前会提前 3.6 倍触发）
- 推送通知里的分钟数（`mins = (durationSec + 59) / 60` → 48s 报「1 分钟」，实际 4 分钟）
- 离线下载记录 / 通勤预加载的时长字段

## 实现

- **WAV**：读 wave 头（采样率 + 帧数）—— 精确。
- **MP3**：逐帧解析帧头，累加每帧时长 —— 精确，且不依赖 ffmpeg
  （生产环境虽有 ffmpeg，但 TTS provider 不保证它可用；纯 Python 更稳）。

MP3 帧头（4 字节）结构：

    AAAAAAAA AAABBCCD EEEEFFGH IIJJKLMM
    A = 帧同步（11 bit，全 1）
    B = MPEG 版本（00=MPEG2.5, 01=reserved, 10=MPEG2, 11=MPEG1）
    C = 层（01=Layer III）
    D = 保护位（0 = 无 CRC）
    E = 比特率索引
    F = 采样率索引
    G = 填充位
    H = 私有位
    I = 声道模式
    J = 模式扩展
    K = 版权
    L = 原始
    M = 强调

每帧时长 = 采样点 / 采样率（MPEG1 Layer III 是 1152，MPEG2/2.5 是 576）。
"""

from __future__ import annotations

import io
import wave

# MPEG 版本（帧头的第 2 字节高 2 位）→ 采样率表
_SAMPLE_RATES: dict[int, list[int]] = {
    3: [44100, 48000, 32000],  # MPEG 1
    2: [22050, 24000, 16000],  # MPEG 2
    0: [11025, 12000, 8000],  # MPEG 2.5
}

# 比特率索引（kbps），按版本分表；None = 非法/自由格式
_BITRATES_V1: list[int | None] = [
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    None,
]
_BITRATES_V2: list[int | None] = [
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    None,
]


def wav_duration_sec(audio: bytes) -> int | None:
    """从 RIFF/WAV 头读时长（秒）；非 WAV 返回 None。"""
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            return int(w.getnframes() / rate) if rate else None
    except Exception:
        return None


def mp3_duration_sec(audio: bytes) -> int | None:
    """逐帧解析 MP3 帧头，累加得到时长（秒）；不是合法 MP3 返回 None。"""
    if len(audio) < 4:
        return None

    pos = 0
    # 跳过 ID3v2 tag（大小是 4 个 syncsafe 字节）
    if audio[:3] == b"ID3":
        if len(audio) < 10:
            return None
        tag_size = 0
        for b in audio[6:10]:
            tag_size = (tag_size << 7) | (b & 0x7F)
        pos = 10 + tag_size
        if pos >= len(audio):
            return None

    total_sec = 0.0
    frames = 0
    size = len(audio)

    while pos + 4 <= size:
        # 帧同步：11 bit 全 1
        if audio[pos] != 0xFF or (audio[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue

        version = (audio[pos + 1] >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (audio[pos + 1] >> 1) & 0x03  # 1=Layer III
        bitrate_idx = (audio[pos + 2] >> 4) & 0x0F
        sr_idx = (audio[pos + 2] >> 2) & 0x03
        padding = (audio[pos + 2] >> 1) & 0x01

        if version == 1 or layer != 1 or sr_idx == 3 or bitrate_idx in (0, 15):
            pos += 1
            continue

        rates = _SAMPLE_RATES[version]
        sample_rate = rates[sr_idx]
        bitrate = (_BITRATES_V1 if version == 3 else _BITRATES_V2)[bitrate_idx]
        if not bitrate or not sample_rate:
            pos += 1
            continue

        # 每帧时长：MPEG1 = 1152/采样率，MPEG2/2.5 = 576/采样率
        samples = 1152 if version == 3 else 576
        total_sec += samples / sample_rate
        frames += 1

        # 跳到下一帧：帧长（字节）
        coeff = 144 if version == 3 else 72
        frame_len = (coeff * bitrate * 1000) // sample_rate + padding
        if frame_len <= 4:
            pos += 4
            continue
        pos += frame_len

    return int(total_sec) if frames else None


def audio_duration_sec(audio: bytes) -> int | None:
    """按格式算音频时长（秒）；无法解析返回 None。

    调用方需要自己决定兜底值 —— 宁可不写，也不要写一个错的。
    """
    if not audio:
        return None
    if audio[:4] == b"RIFF":
        return wav_duration_sec(audio)
    if audio[:3] == b"ID3" or (audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0):
        return mp3_duration_sec(audio)
    return None
