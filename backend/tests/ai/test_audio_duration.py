"""音频时长计算（WAV / MP3）—— 修「字节数 // 32000」的低估。

## 事故

生产上一篇 223.4 秒的音频，`distilled_articles.duration_sec` 记成了 **48**。

原实现：`len(audio) // 32000` —— 假设音频是 32 kbps CBR，
而生产上的 OpenAI 协议 TTS（Qwen3-TTS-12Hz-1.7B-Base）实际码率是 **55 kbps**。

影响不止显示：安卓端 `durationSec` 还用于

- 「听感评分 ≥ 90% 触发」的判定（会提前 3.6 倍触发）
- 推送通知里的分钟数（`(durationSec + 59) / 60` → 48s 报「1 分钟」，实际 4 分钟）
- 离线下载记录 / 通勤预加载的时长字段

修法：按格式精确解析（WAV 读 wave 头；MP3 逐帧解析帧头累加），
解析不出来就不写（None），而不是编一个错的。
"""

import io
import sys
import wave
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts.audio_duration import (  # noqa: E402
    audio_duration_sec,
    mp3_duration_sec,
    wav_duration_sec,
)


def _make_wav(seconds: float, rate: int = 44100) -> bytes:
    """造一段指定时长的 WAV（静音）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _make_mp3_frames(seconds: float, bitrate_kbps: int = 56) -> bytes:
    """造一段指定时长的「合法 MP3 帧序列」（无真实音频数据，只测帧头解析）。

    MPEG1 Layer III / 44100Hz / 56kbps（标准表里最接近生产实测 55179bps 的值）：
      每帧 1152 个采样 → 1152/44100 = 26.12ms
      每帧字节数 = 144 * 56000 / 44100 = 182.8 → 182 (+padding)
    """
    # MPEG1 Layer III 比特率索引表（kbps）：
    #   0=free 1=32 2=40 3=48 4=56 5=64 6=80 7=96 8=112 9=128
    #   10=160 11=192 12=224 13=256 14=320 15=bad
    bitrate_index = {
        32: 1,
        40: 2,
        48: 3,
        56: 4,
        64: 5,
        80: 6,
        96: 7,
        112: 8,
        128: 9,
        160: 10,
        192: 11,
        224: 12,
        256: 13,
        320: 14,
    }[bitrate_kbps]
    frame_len = (144 * bitrate_kbps * 1000) // 44100

    header = bytearray()
    # 帧同步 11bit + MPEG1(11) + Layer III(01) + 无保护(1) → 0xFF 0xFB
    header += bytes([0xFF, 0xFB])
    # 比特率索引(4bit) | 采样率索引 00(44.1kHz) | padding | 私有 → 第三字节
    header += bytes([(bitrate_index << 4) | 0x00, 0xC0])
    out = bytearray(header)
    out += b"\x00" * (frame_len - 4)
    frames = int(seconds / (1152 / 44100))
    return bytes(out) * frames


def test_wav_duration_is_exact():
    audio = _make_wav(3.0)
    assert wav_duration_sec(audio) == 3
    assert audio_duration_sec(audio) == 3


def test_mp3_duration_is_exact():
    audio = _make_mp3_frames(10.0)
    got = mp3_duration_sec(audio)
    assert got is not None
    # 10 秒 → 允许 1 秒误差（帧数取整）
    assert 9 <= got <= 11, f"期望 ~10s，实际 {got}s"


def test_mp3_duration_matches_ffprobe_when_available(tmp_path):
    """有 ffmpeg 时，用真实 mp3 对照 ffprobe 的时长。

    这是关键断言：纯 Python 的帧头解析要和 ffprobe 一致，
    否则详情页显示的时长还是不准。
    """
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("环境没有 ffmpeg/ffprobe，跳过")

    path = tmp_path / "sample.mp3"
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
            "sine=frequency=440:duration=5",
            "-b:a",
            "56k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    audio = path.read_bytes()

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = float(probe.stdout.strip())

    got = mp3_duration_sec(audio)
    assert got is not None
    assert abs(got - expected) <= 1.0, f"ffprobe={expected:.1f}s，纯 Python={got}s"


def test_audio_duration_returns_none_for_garbage():
    """解析不出来就返回 None —— 不能编一个错的时长。"""
    assert audio_duration_sec(b"") is None
    assert audio_duration_sec(b"not audio at all") is None
    assert mp3_duration_sec(b"\x00\x01\x02\x03") is None


def test_duration_is_not_the_byte_size_estimate():
    """回归：不能再出现「字节数 // 32000」那种低估。

    55 kbps 的 mp3：真实时长应约为 字节数/6875 秒，
    而 // 32000 会低估到真实值的 58%。
    """
    audio = _make_mp3_frames(20.0)
    got = audio_duration_sec(audio)
    assert got is not None
    naive = len(audio) // 32000
    assert got > naive * 1.5, f"真实 {got}s 竟然不比旧估算 {naive}s 高 50%"
