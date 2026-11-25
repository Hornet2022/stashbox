"""音色身份契约（2026-11-25）：音域对齐默认关 + 块长放大 + 接缝数。

## 这条契约在说什么

「一篇音频听出好几种音色」这件事上，仓里先后有过两个说法：

1. **上游每次请求换一个说话人** ⇒ 修法 = 让零样本克隆生效（10-24 已做）。
2. **各段音域差一档 ⇒ 听着像换人** ⇒ 修法 = 事后把各段移调到同一音域
   （`voice_align.align_pitch`，10-22 加的）。

第 2 条在 2026-11-25 被**实测推翻**（ECAPA 身份层）：

    上游天然画出的音域差（同文重渲 3 次，音域散布 8.3% = 1.38 半音）
        ⇒ 块间身份余弦 0.972 ~ 0.985   ← 几乎无损
    用对齐把它抹平（各块移 ±0.69 半音）
        ⇒ 块间身份余弦 0.867 ~ 0.940   ← Δ -0.105，明显变差

    单块变调多少半音 ⇒ 身份掉多少：
        0.25 半音 → 0.995    0.5 半音 → 0.983
        1.00 半音 → 0.939    1.5 半音 → 0.879    2.0 半音 → 0.811

⇒ **变调对音色的伤害，比它消除的那点自然音域差大 3 倍。**
   换掉「差异的表达方式」（自然渲染差异 → 人为变调）反而更伤。

⇒ 契约：
    ① **音域对齐默认关闭**（`OPENAI_TTS_ALIGN_PITCH` 默认 "0"）；
    ② 万一打开，**位移封顶 0.5 半音**（染色不可测），超出的段**不动**；
    ③ **块长 800 → 1500**（当前上游实测墙钟 63.0s / 音频 224.3s），
       接缝（= 独立采样次数）随之减少。

## 为什么断言要打在「行为」上

这些判据都住在**运行时行为**里（`align_and_normalize` 到底动没动音频、
`align_pitch` 到底移了多少、`split_into_chunks` 到底切了几块），
所以守卫量的是**行为**（移完之后的基频、切出来的块数），
不是源码里有没有某个字符串 —— 源码字符串会被自己的注释喂饱。

（判据层选错的教训：断言只打在 helper 上，helper 仍对 ⇒ 守卫假绿。
所以每条都打真实调用点。）
"""

from __future__ import annotations

import math
import shutil
import struct
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


def _synth_wav(f0: float, seconds: float = 2.0, rate: int = 16000) -> bytes:
    """造一段已知基频的 WAV（谐波叠加，振幅 1/k）。"""
    import io

    n = int(rate * seconds)
    samples = []
    for i in range(n):
        t = i / rate
        v = sum(math.sin(2 * math.pi * f0 * k * t) / k for k in range(1, 11))
        samples.append(int(max(-1.0, min(1.0, v / 2.0)) * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


def _median_f0(piece: bytes):
    from app.services.tts.voice_align import median_f0

    return median_f0(piece)


# ---------------------------------------------------------------------------
# ① 音域对齐默认关闭（打真实调用点：align_and_normalize）
# ---------------------------------------------------------------------------
def test_音域对齐默认关闭_各段保持原样(monkeypatch):
    """默认不打开对齐 ⇒ 两段**各自的基频原样保留**（不被拉到一起）。

    判据落在 `align_and_normalize`（真正做决定的地方），
    不是 `align_pitch`（helper）—— 断言打 helper 会漏掉
    「调用点忘了传开关」这类改动。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    from app.services.tts.audio_chunks import align_and_normalize

    monkeypatch.delenv("OPENAI_TTS_ALIGN_PITCH", raising=False)

    low = _synth_wav(150.0)
    high = _synth_wav(170.0)
    before = (_median_f0(low), _median_f0(high))

    out = align_and_normalize([low, high])

    assert len(out) == 2
    after = (_median_f0(out[0]), _median_f0(out[1]))
    assert before[0] is not None and before[1] is not None
    # 各段基频基本没动（响度归一只改增益，不改基频）
    for b, a in zip(before, after):
        assert a is not None, "对齐关闭时不该把音频处理坏"
        assert abs(a - b) / b < 0.03, f"对齐默认关闭时基频不该变：{b:.1f} → {a:.1f}"
    # 两段的差距基本还在（没被拉齐）
    assert abs(after[0] - after[1]) > 0.08 * min(after[0], after[1])


def test_显式打开时对齐仍然生效(monkeypatch):
    """显式 `OPENAI_TTS_ALIGN_PITCH=1` ⇒ 对齐**确实**在工作。

    这条是上一条的**反向锚点**：万一有人把开关写死成永远关闭，
    上一条照样绿 ⇒ 必须有这条证明 opt-in 路径没被顺手删掉。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    from app.services.tts.audio_chunks import align_and_normalize

    low = _synth_wav(150.0)
    high = _synth_wav(170.0)

    # ⚠️ **必须同基准比较**：把「关闭」与「开启」两次的**输出**放在一起比。
    #    拿「开启后的输出」去比「开启前的输入」会被响度归一的重编码噪声骗过去
    #    （实测那点噪声就有 0.02Hz，足够让 `after < before` 侥幸成立
    #     ⇒ 守卫变假绿，变异「把开关写死为永远关」抓不到）。
    monkeypatch.delenv("OPENAI_TTS_ALIGN_PITCH", raising=False)
    off = align_and_normalize([low, high])
    gap_off = abs(_median_f0(off[1]) - _median_f0(off[0]))

    monkeypatch.setenv("OPENAI_TTS_ALIGN_PITCH", "1")
    on = align_and_normalize([low, high])
    gap_on = abs(_median_f0(on[1]) - _median_f0(on[0]))

    assert (
        gap_on < gap_off * 0.5
    ), f"显式打开后对齐应当把两段拉近：关 {gap_off:.1f}Hz → 开 {gap_on:.1f}Hz"


# ---------------------------------------------------------------------------
# ② 默认关闭时，各段**不被差异化处理**
# ---------------------------------------------------------------------------
def test_默认关闭时各段不被差异化变调():
    """默认路径下，**任何一段都不该被单独移调**。

    这是本轮实测的核心契约：
        上游天然画出的音域差（同文重渲 1.38 半音）⇒ 块间身份余弦 0.972~0.985
        对齐抹平它（各块移 ±0.69 半音）      ⇒ 块间身份余弦 0.867~0.940
    ⇒ **逐块差异化变调本身**就是「换人感」的一部分来源。

    断言打在 `align_and_normalize` 的真实调用点上（量处理后的基频），
    不是读源码找字符串 —— 后者会被自己的注释喂饱。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    from app.services.tts.audio_chunks import align_and_normalize

    low = _synth_wav(150.0)
    high = _synth_wav(170.0)
    before = (_median_f0(low), _median_f0(high))

    out = align_and_normalize([low, high])

    assert len(out) == 2
    after = (_median_f0(out[0]), _median_f0(out[1]))
    assert before[0] is not None and before[1] is not None
    assert after[0] is not None and after[1] is not None
    for b, a in zip(before, after):
        assert abs(a - b) / b < 0.03, f"默认路径不该动任何一段的基频：{b:.1f} → {a:.1f}"


# ---------------------------------------------------------------------------
# ③ 块长 → 接缝数（接缝 = 独立采样次数 = 「听出好几种音色」的来源）
# ---------------------------------------------------------------------------
def test_块长放大后接缝数下降():
    """2500 字的稿子 ⇒ 块长 1500 时 **≤2 块**（接缝 ≤1）。

    生产实测（当前上游）：1500 字 → 墙钟 63.0s / 音频 224.3s，
    对我们自己的 httpx 读超时（300s）留 4.8 倍余量，
    且远低于上游实测的 ~322s 音频上限（1600/2000/2600 三档音频时长都精确等于 322.0s）。
    """
    from app.services.tts.audio_chunks import split_into_chunks
    from app.services.tts.openai import _CLONE_CHUNK_CHARS_DEFAULT

    # 17 字 × 150 = 2550 字：块长 1500 ⇒ 2 块；块长 800 ⇒ 4 块（这条会红）
    text = "这是一段用来测分块的中文测试文本。" * 150
    chunks = split_into_chunks(text, _CLONE_CHUNK_CHARS_DEFAULT)

    assert len(chunks) <= 2, (
        f"{len(text)} 字在块长 {_CLONE_CHUNK_CHARS_DEFAULT} 下切了 {len(chunks)} 块 —— "
        "接缝是「一篇听出好几种音色」的直接来源"
    )
    assert all(len(c) <= _CLONE_CHUNK_CHARS_DEFAULT for c in chunks)


def test_接缝数不超过结构上限():
    """块长 1500 ⇒ 3000 字的稿子 ≤ 3 块，仍在 `MAX_SEGMENTS = 4` 以内。"""
    from app.services.tts.audio_chunks import split_into_chunks
    from app.services.tts.consistency import MAX_SEGMENTS
    from app.services.tts.openai import _CLONE_CHUNK_CHARS_DEFAULT

    # 17 字 × 180 = 3060 字：块长 1500 ⇒ 3 块；块长 800 ⇒ 4 块（这条会红）
    text = "这是一段用来测分块的中文测试文本。" * 180
    chunks = split_into_chunks(text, _CLONE_CHUNK_CHARS_DEFAULT)

    assert len(chunks) <= MAX_SEGMENTS, f"3000 字切了 {len(chunks)} 块，超过结构上限 {MAX_SEGMENTS}"
