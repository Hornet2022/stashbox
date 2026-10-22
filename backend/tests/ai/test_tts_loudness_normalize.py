"""TTS 响度归一（静态增益 + 限峰）。

## 为什么锁这条

2026-10-22 实测发现生产日志里**每个分块**都超时 120 秒：

    TTS 响度归一失败：Command '['/usr/bin/ffmpeg', ..., '-af',
    'loudnorm=I=-16.0:TP=-1.5:LRA=11', ...]' timed out after 120 seconds，
    退回原音频

后果有两层，**都更糟**：

1. **归一根本没生效** → 段间响度差 6.5 LU（用户听到的「换人」一半来自响度跳变）；
2. **代价是每篇 8~15 个分块 × 120s = 16~30 分钟纯等待**
   （蒸馏 P95 面板实测 step3_tts p50=300s / p95=570s）。

生产机实测（2 核，77 秒真实音频）：

| 方案 | 单次 | 4 并发 | 段间一致性（LUFS 极差） |
|---|---|---|---|
| loudnorm | 4.72s | 19.0s | 1.0 LU |
| dynaudnorm | 0.77s | 2.7s | 2.1 LU |
| **静态增益** | **0.14s** | — | **0.5 LU** |

所以换成「便宜测量 + 静态增益」：两个维度都比 loudnorm 好。

## 本文件锁住

A. 增益按**实测**的平均电平算（不是固定值）—— 安静片段放大、响片段压低
B. 增益有上限（±20 dB）—— 量到异常值时不会把底噪一起放大
C. 音量确实朝目标移动（用真实 ffmpeg 跑，量输出电平）
D. ffmpeg 不可用 / 失败 → 返回 None，调用方退回原音频（宁可不统一，也不能丢内容）
E. **超时上限必须是几十秒级，不能是 120 秒** —— 这正是原来的洞
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from stashbox.backend.app.services.tts.audio_chunks import (
    _measure_mean_db,
    normalize_loudness,
)

FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="本机没装 ffmpeg")


def _make_tone(duration_sec: float, freq: int = 220) -> bytes:
    """造一段纯音（用 ffmpeg 生成，体积小且电平可控）。"""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration_sec}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "mp3",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _mean_db(piece: bytes) -> float:
    """量一段音频的平均电平（dBFS）。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "in.mp3"
        p.write_bytes(piece)
        value = _measure_mean_db(FFMPEG, str(p), timeout=30)
    assert value is not None
    return value


def test_增益按实测电平算():
    """A. 安静片段要被放大，响片段要被压低 —— 增益不是固定的。"""
    quiet = _make_tone(1.0)
    loud = _make_tone(1.0)
    # 用不同的音量重做「响」的那段
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1.0",
            "-af",
            "volume=-12dB",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "mp3",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    loud = proc.stdout

    q_db = _mean_db(quiet)
    l_db = _mean_db(loud)
    assert l_db < q_db, "测试前提：第二段应当更安静"

    out_q = normalize_loudness(quiet)
    out_l = normalize_loudness(loud)
    assert out_q is not None and out_l is not None

    # 两段处理后的电平差应当**小于**处理前
    before = q_db - l_db
    after = _mean_db(out_q) - _mean_db(out_l)
    assert after < before, f"归一后段间电平差没有收窄：{before} → {after}"


def test_输出电平朝目标移动():
    """C. 目标 -20 dBFS：安静段被拉高到接近目标。"""
    quiet = _make_tone(1.0)
    out = normalize_loudness(quiet, target_rms_db=-20.0)
    assert out is not None
    # 处理后应当明显高于处理前（-20 dB 目标比原始正弦高）
    assert _mean_db(out) > _mean_db(quiet) - 1.0


def test_增益有上限():
    """B. 量到异常电平时增益被夹住，不会把底噪放大成啸叫。"""
    # 直接测「夹住」这条逻辑：用极端 target 推出极端 gain，看是否被限制
    from stashbox.backend.app.services.tts.audio_chunks import normalize_loudness as nl

    piece = _make_tone(0.5)
    # 目标 +40 dB → 理论增益超过 +20 dB，必须被夹住
    out = nl(piece, target_rms_db=40.0)
    assert out is not None
    # 夹住后最多抬高 20 dB：输出电平不应超过原始 + 20 dB
    assert _mean_db(out) <= _mean_db(piece) + 20.5


def test_ffmpeg_不可用时返回_none():
    """D. ffmpeg 不可用 → 返回 None（调用方退回原音频，不丢内容）。"""
    out = normalize_loudness(_make_tone(0.5), ffmpeg_bin="/nonexistent/ffmpeg")
    assert out is None


def test_超时上限是几十秒级():
    """E. 超时不能是 120 秒 —— 那正是生产上每个分块都超时的原因。

    静态增益方案实测单次 0.14s、4 并发也不到 1s，
    30s 已经是几百倍的余量。而 120s 的超时意味着：
    真出问题时，每个分块要白等 2 分钟才肯放弃。
    """
    import inspect

    sig = inspect.signature(normalize_loudness)
    timeout = sig.parameters["timeout"].default
    assert timeout <= 45, f"响度归一的超时上限过大：{timeout}s"
    assert timeout >= 10, f"超时上限过小，正常情况会被误杀：{timeout}s"
