"""**流水线级**的跨段音域对齐（2026-10-24）。

## 这个用例在守什么

内测反馈：「一篇音频大概能明显听出来有三种音色」。

根因（实测出来的，不是猜的）：

* step3 把稿子按 hook / 小节 / 句子切成 N 段，**每段是一次独立的 TTS 请求**
  ⇒ 上游每次独立采样 ⇒ 各段音域各飘各的
  （实测 段1 p25=152.1Hz / 段2=169.2Hz / 段3=133.2Hz，**差一档**）；
* 而「音高对齐 + 响度归一」这套处理原先**只长在 provider 内部的
  `concat_audio` 里** —— 只在「单次请求 >700 字自己切段」时才生效；
  **流水线级的段之间完全没做** —— 而那正是听感上「换人」的地方。

⇒ 护栏要锁两件事：
1. `align_and_normalize` 是**一份实现**（provider 内部与流水线拼接共用）；
2. **流水线拼接前必须调用它**（否则这个 bug 会以「代码还在」的形态复活）。

## 为什么不做 crossfade

step4 的每段带**各自的停顿**（hook 后 700ms / 节间 500ms / 段内 200ms /
末段 600ms 收尾）—— 那是听感的呼吸感，crossfade 会把它吃掉。
而「换人感」的根因是**音域差一档**，音高对齐正好治它。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_SERVICE_DIR = BACKEND_DIR / "ai-service"
if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))

from app.services.tts.audio_chunks import (  # noqa: E402
    align_and_normalize,
    concat_audio,
)
from app.services.tts import voice_align  # noqa: E402


def test_align_and_normalize_存在且是公开函数():
    """它必须能被**流水线**调用（原来只在 provider 内部有这套逻辑）。"""
    assert callable(align_and_normalize)
    assert align_and_normalize.__doc__, "要写清楚为什么需要它"


def test_concat_audio_复用同一份实现():
    """provider 内部与流水线拼接必须走**同一份**判据（两份实现必然漂移）。

    ⚠️ 对齐逻辑住在 `concat_mp3` 里（`concat_audio` 只是 WAV/MP3 的分派器），
    所以断言的是**真正做拼接的那个函数**。
    """
    src = (BACKEND_DIR / "app" / "services" / "tts" / "audio_chunks.py").read_text(encoding="utf-8")
    body = src.split("def concat_mp3(")[1].split("\ndef ")[0]
    assert "align_and_normalize(pieces)" in body, (
        "concat_mp3 必须复用 align_and_normalize —— "
        "两份实现会漂移（这正是本 bug 的形态：逻辑长在别处没人发现）"
    )


def test_对齐失败时保留原音频不丢内容():
    """对齐/归一失败 ⇒ 退回原音频（宁可音色不齐，也不能丢内容）。"""
    pieces = [b"fake-mp3-a", b"fake-mp3-b"]

    # 强制对齐失败：把 align_pitch 换成会抛异常的替身
    orig = voice_align.align_pitch

    def _boom(pieces, ffmpeg_bin=None):
        raise RuntimeError("模拟对齐失败")

    voice_align.align_pitch = _boom
    try:
        out = align_and_normalize(pieces)
    finally:
        voice_align.align_pitch = orig

    assert len(out) == len(pieces), "失败时不能丢段"
    assert all(isinstance(p, (bytes, bytearray)) for p in out)


def test_空输入不炸():
    assert align_and_normalize([]) == []


def test_单段时原样返回():
    """只有一段时没有「跨段」可言，不该做任何处理。"""
    one = [b"only"]
    assert align_and_normalize(one) == one


def test_流水线拼接前调用了对齐():
    """step4（流水线拼接）必须调用 align_and_normalize。

    这是这个 bug 的**本体**：逻辑曾经只长在 provider 内部，
    流水线这一层完全没做 ⇒ 内测听到「三种音色」。
    """
    src = (BACKEND_DIR / "ai-service" / "distill" / "steps.py").read_text(encoding="utf-8")
    assert "align_and_normalize" in src, (
        "step4 必须做跨段音域对齐 —— 否则每段独立采样带来的音域差一档"
        "（152/169/133Hz）会原样留在成品音频里"
    )
    # 必须在**拼接之前**（拼接后就只剩一个文件，没法再对齐了）
    align_at = src.index("align_and_normalize")
    # step4 里的首次拼接调用点
    concat_at = src.index("def step4_concat")
    assert align_at > concat_at, "对齐要在 step4 里做"
    # 拼接实现（_concat_wavs_with_per_segment_gaps / ffmpeg concat）在 step4 里更靠后
    assert align_at < src.index("_concat_wavs_with_per_segment_gaps(", concat_at)
