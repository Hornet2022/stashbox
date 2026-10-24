"""段落音高对齐：估计器校准 + 对齐行为（2026-10-22）。

## 为什么先校准

实测发现同一个音频文件，用两套参数（8kHz/50ms 帧 vs 4kHz/200ms 帧）
测出的中位基频差 18%（153.8Hz vs 181.8Hz）。指标本身不可靠的话，
基于它的所有决策都是空中楼阁 —— 所以先拿**已知 F0 的合成信号**
把估计器校准到可用的精度，再谈用它去改生产音频。

## 判据

合成信号是 f0 的谐波叠加（振幅 1/k），基频已知 → 估计误差必须小。
人声基频范围 60~400Hz，测 100/150/200/250 四档。
"""

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


@pytest.fixture
def voice_align():
    from app.services.tts import voice_align as mod

    return mod


@pytest.mark.parametrize("f0", [100.0, 120.0, 150.0, 170.0, 200.0, 250.0])
def test_median_f0_recovers_known_pitch(voice_align, f0):
    """估计器必须在已知基频上准确（误差 ≤ 5%）。

    5% 的容忍度来自实测：同一个音频换一套帧参数就会差 18%，
    所以这里要求的是**同一套参数下的可重复性 + 绝对精度**。

    170Hz 这一档是**八度错误的照妖镜**：自相关峰值法在它上面
    会报 85.11Hz（正好一半），因为 2T 处的相关峰更高。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    measured = voice_align.median_f0(_synth_wav(f0))

    assert measured is not None, f"合成的 {f0}Hz 信号应该测得出基频"
    error = abs(measured - f0) / f0
    assert error <= 0.05, f"实测 {measured:.1f}Hz vs 真值 {f0}Hz，误差 {error:.1%} 超过 5%"


def test_median_f0_does_not_halve_the_pitch(voice_align):
    """回归：不得把基频报成一半（八度错误）。

    这不是理论风险 —— 自相关峰值法在 170Hz 上实测报 85.11Hz。
    报成一半会让「段间基频差」的判断完全失真：
    121Hz 会被当成 60Hz 段、160Hz 会被当成 80Hz 段，
    对齐目标也就选错了。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    measured = voice_align.median_f0(_synth_wav(170.0))

    assert measured is not None
    assert measured > 140.0, f"170Hz 被测成 {measured:.1f}Hz —— 八度错误"


def test_median_f0_is_repeatable(voice_align):
    """同一段音频测两次必须得到同一个值（可重复性）。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    audio = _synth_wav(180.0)
    a = voice_align.median_f0(audio)
    b = voice_align.median_f0(audio)
    assert a is not None and b is not None
    assert abs(a - b) / a < 0.01, f"两次测量不一致：{a} vs {b}"


def test_register_f0_uses_median_not_p25(voice_align):
    """`register_f0` 是**音域锚点**，用**中位数**而不是 p25（2026-10-24 改）。

    ## 为什么要改

    原来是 p25，理由是「中位数会被真实语音 F0 分布的上尾带跑」。
    **这条理由在克隆打通之后不成立了** —— 而 p25 本身是个坏用的锚点：
    它是**尾部统计量**，对「这一块里有多少帧落在低频区」极敏感
    （气声、句尾下沉、辅音间隙），同一说话人的不同块就能差 26%
    （实测：生产的 5 块真实渲染件，p25 散布 26.0% vs 中位数散布 5.6%）。
    拿它当锚点 → 各块算出的位移量都是错的 → 对齐完反而把本来
    一致的块**推散**（散布 5.6% → 14.3%，相邻块音色距离 0.0085 → 0.0641）。

    ## 这里的断言就是锁住「用中位数」这个选择

    造一段**前半 150Hz、后半 300Hz** 的信号：
      · p25   → 150 Hz（落在低的那一半）
      · 中位数 → 225 Hz（两半的中间）
    ⇒ `register_f0` 必须报 ~225Hz。报 150 就说明又退回 p25 了。
    """
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    def _synth_two_halves(
        f0_low: float, f0_high: float, seconds: float = 4.0, rate: int = 16000
    ) -> bytes:
        import io
        import struct
        import wave

        n = int(rate * seconds)
        samples = []
        for i in range(n):
            t = i / rate
            f = f0_low if t < seconds / 2 else f0_high
            v = sum(math.sin(2 * math.pi * f * k * t) / k for k in range(1, 11))
            samples.append(int(max(-1.0, min(1.0, v / 2.0)) * 32767))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack(f"<{n}h", *samples))
        return buf.getvalue()

    audio = _synth_two_halves(150.0, 300.0)
    measured = voice_align.register_f0(audio)
    assert measured is not None
    # 中位数 ≈ 225（p25 会给 150）
    assert 195 <= measured <= 255, f"锚点实测 {measured:.1f}Hz，应接近中位数 225Hz（p25 会给 150）"


def test_register_f0_survives_strong_harmonics(voice_align):
    """锚点不能被强高次谐波带跑（真值 150Hz 的信号仍应报 ~150Hz）。

    ⚠️ 这条曾经是「必须用 p25」的理由，但实测**中位数口径也过**（150.0Hz）
    —— 自相关估计器对这类信号不会把中位数带跑。
    当年的对照样本是「同一段文字两次合成」—— 那是**换了说话人**，
    不是「表达把中位数带跑」，两件事被混为一谈了。
    留着这条用例：它守住的是**估计器不被谐波污染**，与用哪个分位无关。
    """
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    def _synth_with_strong_harmonics(f0: float, seconds: float = 2.0, rate: int = 16000) -> bytes:
        import io
        import struct
        import wave

        n = int(rate * seconds)
        samples = []
        for i in range(n):
            t = i / rate
            # 第 3、5、7 次谐波压过基波
            v = (
                0.6 * math.sin(2 * math.pi * f0 * t)
                + 1.0 * math.sin(2 * math.pi * f0 * 3 * t)
                + 0.8 * math.sin(2 * math.pi * f0 * 5 * t)
            )
            samples.append(int(max(-1.0, min(1.0, v / 3.0)) * 32767))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack(f"<{n}h", *samples))
        return buf.getvalue()

    audio = _synth_with_strong_harmonics(150.0)
    measured = voice_align.register_f0(audio)
    assert measured is not None
    error = abs(measured - 150.0) / 150.0
    assert error <= 0.05, f"音域锚点实测 {measured:.1f}Hz vs 真值 150Hz，误差 {error:.1%}"


def _synth_wav_with_low_frames(
    f0: float, low_f0: float, low_ratio: float, seconds: float = 4.0, rate: int = 16000
) -> bytes:
    """造一段基频 f0、但有一部分帧掉到 low_f0 的 WAV。

    模拟真实语音里的**气声 / 句尾下沉 / 辅音间隙**：
    它们把 F0 分布的**下缘**拉低，但**中位数不变** ——
    也就是说，这一段的「说话人音域」没变，只是分布的尾巴变了。

    ⇒ 用 p25 当锚点时，这段会被误判成「音域低一档」（实测 p25 差 25%）。
    """
    import io
    import struct
    import wave

    n = int(rate * seconds)
    samples = []
    n_low = int(n * low_ratio)
    for i in range(n):
        t = i / rate
        f = low_f0 if i < n_low else f0
        v = sum(math.sin(2 * math.pi * f * k * t) / k for k in range(1, 11))
        samples.append(int(max(-1.0, min(1.0, v / 2.0)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


def _median_spread(mod, pieces: list[bytes]) -> float:
    """各段中位基频的散布（%）。"""
    medians = [mod.median_f0(p) for p in pieces]
    vals = [m for m in medians if m]
    if len(vals) < 2:
        return 0.0
    return (max(vals) - min(vals)) / min(vals) * 100


def test_align_pitch_never_increases_measured_spread(voice_align):
    """**不变式：对齐不得让各段的基频散布变大。**

    这条锁的是 2026-10-24 定位到的真 bug：
    锚点用 p25 时，同一个说话人的三块（气声比例不同）被算成三个不同音域
    ⇒ 位移量各自不同 ⇒ **把本来一致的块推散**（实测散布 0% → 33%）。

    合成信号刻意复刻那个形状：基频一样，只有低频帧的比例不同
    （= 气声 / 句尾下沉）。此时
      · 中位数三块几乎相同（说话人没变）
      · p25 三块差一档（尾巴变了）
    ⇒ 用中位数当锚点就不会动它们；用 p25 就会把它们推散。
    """
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    pieces = [
        _synth_wav_with_low_frames(200.0, 150.0, 0.10),
        _synth_wav_with_low_frames(200.0, 150.0, 0.30),
        _synth_wav_with_low_frames(200.0, 150.0, 0.05),
    ]

    before = _median_spread(voice_align, pieces)
    out, _ = voice_align.align_pitch(pieces)
    after = _median_spread(voice_align, out)

    assert (
        after <= before + 1.0
    ), f"对齐把一致性搞坏了：散布 {before:.1f}% → {after:.1f}%（对齐不得让散布变大）"


def test_align_pitch_still_fixes_a_real_register_difference(voice_align):
    """改用中位数锚点后，**真正的音域差仍然要被对齐掉**。

    上一条守住「不许弄坏」，这条守住「还得管用」——
    上游返回不同说话人时（那正是本步骤存在的理由），两段要被拉到同一音域。
    """
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    low = _synth_wav(150.0)
    high = _synth_wav(170.0)

    before = _median_spread(voice_align, [low, high])
    out, details = voice_align.align_pitch([low, high])
    after = _median_spread(voice_align, out)

    assert before > 5.0, f"构造的样本本身应差一档（实测 {before:.1f}%）"
    assert after <= before, f"音域差没有被对齐掉：{before:.1f}% → {after:.1f}%"
    assert any(d["semitones"] for d in details), "应当真的发生了位移"


def test_align_pitch_moves_segment_towards_target(voice_align, tmp_path):
    """对齐后，各段的基频应该**真的**靠近目标（不是越对齐越离谱）。

    曾经的 bug：符号写反，181.8Hz 被推到 235.3Hz —— 越对齐越离谱。
    这个用例就是为了挡住那种错误。
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    low = _synth_wav(150.0)
    high = _synth_wav(170.0)

    out, details = voice_align.align_pitch([low, high], target_f0=160.0)

    assert len(out) == 2
    assert len(details) == 2
    # 两段都要往 160 靠
    for piece in out:
        f0 = voice_align.median_f0(piece)
        assert f0 is not None
        assert abs(f0 - 160.0) / 160.0 <= 0.06, f"对齐后基频 {f0:.1f}Hz 偏离目标 160Hz 过多"
    # 明细里要有 before/after，供日志观测
    assert all("f0_before" in d and "f0_after" in d for d in details)


def test_align_pitch_skips_when_shift_exceeds_cap(voice_align):
    """位移超过封顶 → **不动**（人工感比不同人更糟）。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    low = _synth_wav(120.0)
    high = _synth_wav(120.0)

    out, details = voice_align.align_pitch(
        [low, high], target_f0=120.0 * 2 ** (4.0 / 12.0), max_semitones=2.5
    )

    assert details[0].get("skipped") == "over_cap"
    assert out[0] == low, "超封顶的段应原样保留"


def test_align_pitch_noop_for_single_piece(voice_align):
    """单段不需要对齐（没有「段间差异」这回事）。"""
    audio = _synth_wav(200.0)
    out, details = voice_align.align_pitch([audio])
    assert out == [audio]
    assert details == []


def test_align_pitch_degrades_when_ffmpeg_missing(voice_align, monkeypatch):
    """ffmpeg 不可用 → 原样返回，不抛异常（不能因为对齐失败而丢音频）。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    monkeypatch.setenv("FFMPEG_BIN", "/nonexistent/ffmpeg")
    pieces = [_synth_wav(150.0), _synth_wav(170.0)]

    out, _ = voice_align.align_pitch(pieces, target_f0=160.0)

    assert out == pieces


def test_rubberband_pitch_argument_is_a_ratio_not_semitones(voice_align, monkeypatch):
    """rubberband 的 `pitch` 必须是**频率倍率**，不是半音。

    ffmpeg 的 rubberband 滤镜里 `pitch` 的取值范围是 [0.01, 100]，
    它是「音高乘以几倍」。直接传半音（尤其负的）会被 ffmpeg 判
    "Value -0.840000 for parameter 'pitch' out of range" → 整条滤镜失败
    → 悄悄回落到 asetrate 兜底。

    这个 bug 实测踩到过：服务器日志里全是「rubberband 不可用」，
    用的根本不是质量最好的那条路径。

    本机 ffmpeg 没有 rubberband 滤镜，所以这里断言的是**参数构造**，
    而不是滤镜行为。
    """
    import subprocess

    captured: list[list[str]] = []
    real_run = subprocess.run

    def _fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "rubberband" in " ".join(cmd):
            captured.append(cmd)
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(voice_align.subprocess, "run", _fake_run)

    voice_align._shift_pitch(_synth_wav(150.0), 1.0)

    assert captured, "应该尝试过 rubberband"
    af = next(a for a in captured[0] if a.startswith("rubberband="))
    value = float(af.rsplit("=", 1)[1])

    # 1 个半音 = 2^(1/12) ≈ 1.0595
    assert value == pytest.approx(2 ** (1.0 / 12.0), rel=1e-4)
    # 必须在 ffmpeg 允许的区间内
    assert 0.01 <= value <= 100


def test_rubberband_is_used_when_available(voice_align):
    """服务器上的 ffmpeg 有 rubberband —— 必须真的走那条路（质量最好）。"""
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("环境没有 ffmpeg，跳过")

    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "rubberband" not in (probe.stdout or ""):
        pytest.skip("本机 ffmpeg 没有 rubberband 滤镜")

    audio = _synth_wav(150.0)
    shifted = voice_align._shift_pitch(audio, 12.0)  # +1 个八音
    assert shifted is not None
    measured = voice_align.median_f0(shifted)
    assert measured is not None
    assert abs(measured - 300.0) / 300.0 <= 0.08, f"一个八音后应约 300Hz，实测 {measured:.1f}Hz"
