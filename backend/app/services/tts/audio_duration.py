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
    d = wav_duration_exact_sec(audio)
    return int(d) if d is not None else None


def wav_duration_exact_sec(audio: bytes) -> float | None:
    """WAV 精确时长（秒，保留小数）。

    ⚠️ **为什么要「精确版」**（2026-10-24 实测）：
    截断判据用「字数 / 时长」算语速，时长被取整到整秒时，
    一个 17s 的块有 ±0.5s 的量化误差 → 语速误差 ±3%，17s 上就是 ±0.2 字/秒 ——
    而 250 字块的正常上界是 6.25 字/秒、阈值是 6.5，**余量只有 0.25**：
    量化误差和余量同量级，判据就成了掷骰子。
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            return (w.getnframes() / rate) if rate else None
    except Exception:
        return None


def mp3_info(audio: bytes) -> tuple[float, int, int] | None:
    """解析 MP3 → (时长秒, 采样率, 声道数)；不是合法 MP3 返回 None。

    ⚠️ 时长是**小数**（不取整）—— 判据要用，见 `audio_duration_exact_sec`。

    声道数从**第一帧**的声道模式读（00=单声道, 01=双声道, 10/11=单声道）。

    为什么需要声道数：拼接时要插入一段**参数完全一致**的静音 mp3
    （段间停顿），ffmpeg 的 concat demuxer 要求各输入的编码参数一致，
    否则会重采样/重编码（`-c copy` 就不成立了）。
    """
    if len(audio) < 4:
        return None

    pos = 0
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
    sample_rate = 0
    channels = 1

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
        rate = rates[sr_idx]
        bitrate = (_BITRATES_V1 if version == 3 else _BITRATES_V2)[bitrate_idx]
        if not bitrate or not rate:
            pos += 1
            continue

        if not sample_rate:
            sample_rate = rate
            # 声道模式（第 4 字节的高 2 位）：01 = 双声道，其余 = 单声道
            mode = (audio[pos + 3] >> 6) & 0x03
            channels = 2 if mode == 1 else 1

        # 每帧时长：MPEG1 = 1152/采样率，MPEG2/2.5 = 576/采样率
        samples = 1152 if version == 3 else 576
        total_sec += samples / rate
        frames += 1

        # 跳到下一帧：帧长（字节）
        coeff = 144 if version == 3 else 72
        frame_len = (coeff * bitrate * 1000) // rate + padding
        if frame_len <= 4:
            pos += 4
            continue
        pos += frame_len

    if not frames:
        return None
    # ⚠️ 时长保留小数（不再 int()）：截断判据拿它算语速，
    # 取整到整秒会让 17s 的块有 ±3% 的语速误差 —— 而判据余量只有 ±4%。
    return total_sec, sample_rate, channels


def mp3_duration_sec(audio: bytes) -> int | None:
    """逐帧解析 MP3 帧头，累加得到时长（**整秒**）；不是合法 MP3 返回 None。

    整秒是给「展示 / 落库」用的（DB 列是 int，客户端也按整秒算）。
    要小数请用 `mp3_duration_exact_sec`。
    """
    d = mp3_duration_exact_sec(audio)
    return int(d) if d is not None else None


def mp3_duration_exact_sec(audio: bytes) -> float | None:
    """逐帧解析 MP3 帧头，累加得到时长（秒，保留小数）；不是合法 MP3 返回 None。"""
    info = mp3_info(audio)
    return info[0] if info else None


def audio_duration_sec(audio: bytes) -> int | None:
    """按格式算音频时长（**整秒**）；无法解析返回 None。

    调用方需要自己决定兜底值 —— 宁可不写，也不要写一个错的。
    """
    d = audio_duration_exact_sec(audio)
    return int(d) if d is not None else None


def audio_duration_exact_sec(audio: bytes) -> float | None:
    """按格式算音频时长（秒，保留小数）；无法解析返回 None。

    ## 谁该用哪个

    - **落库 / 展示**（`duration_sec` 列、客户端时长）→ `audio_duration_sec`（整秒）。
    - **判据**（语速/截断）→ `audio_duration_exact_sec`（小数）。

    为什么判据必须用小数：语速 = 字数 / 时长，时长取整到整秒时，
    17s 的块有 ±0.5s 量化误差 → 语速误差 ±3%；250 字块的正常上界是
    6.25 字/秒、阈值 6.5，**余量只有 0.25 字/秒** —— 与量化误差同量级。
    """
    if not audio:
        return None
    if audio[:4] == b"RIFF":
        return wav_duration_exact_sec(audio)
    if audio[:3] == b"ID3" or (audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0):
        return mp3_duration_exact_sec(audio)
    return None
