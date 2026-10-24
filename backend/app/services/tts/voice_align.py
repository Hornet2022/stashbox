"""段落音域对齐（voice alignment）—— 把各段统一到同一个音域。

## 它解决什么

上游是 zero-shot 克隆，**每次请求都重新采样** → 一篇稿子分段合成后，
各段的**音域**会飘一档。实测（2026-10-22，art_e044b3b1bd6c4fbe90d4c628
的 3 段真实渲染件）：

    段1 p25=152.1Hz   段2 p25=169.2Hz   段3 p25=133.2Hz

差 12%~27% —— 听感上就是「换了个音色」。crossfade 只能磨掉接缝的
「咔哒」声，磨不掉这个。

这里把各段统一到全篇的共同音域锚点（各段 p25 的**中位数**），
位移用 ffmpeg rubberband（只变调、不动共振峰）。

实测效果：152.1 / 169.2 / 133.2 → 152.1 / 152.7 / 149.5Hz（误差 ≤1.8%）。

## 两个必须踩过才知道的坑

1. **度量用 p25，不能用中位数。**
   真实 TTS 语音的 F0 分布很宽（94~570Hz），中位数被随机采样的上尾带跑
   —— 同一段文字合成两次，中位数能从 121 跳到 167（**纯随机**）。
   p25 落在「说话人音域的下缘」，那才是身份特征。
   之前用中位数时，位移后测出 304Hz（预期 ~188Hz）—— 方向完全错。

2. **F0 搜索上界要放到 600Hz，不能是 400。**
   真实语音的 F0 能到 570Hz（实测 max 571/577/573），
   卡在 400 会把高音部分整片丢掉 → 统计量被带偏 → 位移方向判错。

## 实现要点

- 纯标准库（生产服务器没有 numpy/scipy）
- 基频估计用 **YIN**（累积均值归一化差分 + 取第一个过阈值的谷），
  不用自相关峰值法 —— 后者会犯**八度错误**（真实 170Hz 报成 85.11Hz）
- ffmpeg rubberband 的 `pitch` 是**频率倍率**不是半音（范围 [0.01, 100]），
  传半音会被判越界、整条滤镜失败并静默回落
- rubberband 不可用时退回 asetrate+atempo（任何 ffmpeg 都有）
- 位移封顶 3.5 半音，超过就不动（人工感比不同人更糟）
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# 分析用采样率。基频范围 60~400Hz，4kHz 采样绰绰有余，
# 而且帧长与滞后数都随之减半 → 自相关快 4 倍。
_ANALYSIS_RATE = 4000

# 基频搜索范围（Hz）。
#
# ⚠️ 上界必须是 600 而不是 400：真实 TTS 语音的 F0 分布能到 570Hz
# （实测同一篇稿子 3 段的 max 是 571/577/573Hz），范围卡在 400 会把
# 高音部分**整片丢掉** → 统计量被带偏 → 位移方向判错。
# 实测卡 400 时，位移后测出的中位是 304Hz（预期 ~188Hz）—— 就是这么来的。
# 下界 50：低于 50 基本是噪声。
_F0_MIN = 50.0
_F0_MAX = 600.0

# 分析帧长与步进（秒）。200ms 步进对 100+ 秒的音频仍有 500+ 帧，
# 中位数足够稳；再密只是白烧 CPU。
_FRAME_SEC = 0.2

# YIN 的判定阈值：d'(τ) 跌到它以下就算「这里有周期」。
# YIN 论文的推荐值是 0.10~0.15；取 0.15（偏高 → 更保守，
# 宁可少采信几帧，也不要把清音当成浊音）。
_YIN_THRESHOLD = 0.15

# 位移死区：偏离目标在这个比例以内不动。
#
# 取 3%（≈0.5 个半音）—— 0.5 个半音以下人耳基本听不出，
# 超过就能听出「不是同一个人在说」。曾经取 8%（1.37 个半音），
# 那个量级**已经能被听出来了**，等于把要解决的问题放过去了。
_DEADBAND_RATIO = 0.03

# 位移封顶（半音）。超过就不动 —— 变调太多人工感强，
# 「不像同一个人」比「明显被处理过」更糟。
#
# 实测（2026-10-22，art_e044b3b1bd6c4fbe90d4c628 的 3 段真实渲染件）：
#     段1 p25=152.1Hz  段2 p25=169.2Hz  段3 p25=133.2Hz
#     位移后 → 152.1 / 152.7 / 149.5Hz（误差 ≤1.8%）
# 实际需要的位移是 -1.84 / +2.30 半音 → 封顶 3.5 留一点余量。
_MAX_SHIFT_SEMITONES = 3.5


def _decode_pcm(audio: bytes, ffmpeg_bin: str | None = None) -> list[float]:
    """把音频解码成 4kHz 单声道 float 样本列表。失败返回空列表。"""
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="tts_f0_") as tmp:
            src = os.path.join(tmp, "in.mp3")
            Path(src).write_bytes(audio)
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    src,
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(_ANALYSIS_RATE),
                    "-",
                ],
                capture_output=True,
                timeout=180,
                check=True,
            )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        log.warning("TTS 基频分析失败（解码）: %s", exc)
        return []

    raw = proc.stdout
    if not raw:
        return []
    count = len(raw) // 2
    if count == 0:
        return []
    # 16bit 小端 → float。用 array 批量解包比逐个 struct.unpack 快。
    import array

    samples = array.array("h")
    samples.frombytes(raw[: count * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return [s / 32768.0 for s in samples]


def _frame_f0(frame: list[float]) -> float | None:
    """一帧的基频。**用 YIN**（累积均值归一化差分），不用裸自相关。

    ## 为什么必须用 YIN 而不是自相关峰值法

    自相关取全局最大峰会犯**八度错误**：真实基频 170Hz 时，
    2T（340Hz）处的相关峰常常比 T 处更高 → 报出 85Hz（正好一半）。

    实测踩到过：同一段 170Hz 的合成信号，自相关法测出 85.11Hz；
    同一个音频换一套帧参数就测出 153.8Hz vs 181.8Hz（差 18%）。
    指标不可信 → 基于它的所有决策都是空中楼阁。

    YIN 的做法：先算差分函数 d(τ)，再做**累积均值归一化**
    d'(τ) = d(τ)·τ / Σ_{j≤τ} d(j)，然后取**第一个**跌到阈值以下的
    局部极小 —— 「第一个」正是它避开八度错误的关键（τ 越小越好，
    但要真的像周期）。

    最后做抛物线插值取亚采样精度。

    非浊音（清音/静音）返回 None。
    """
    n = len(frame)
    min_lag = max(2, int(_ANALYSIS_RATE / _F0_MAX))
    max_lag = min(n - 1, int(_ANALYSIS_RATE / _F0_MIN))
    if max_lag <= min_lag:
        return None

    mean = sum(frame) / n
    x = [v - mean for v in frame]
    sq = [v * v for v in x]
    pre = [0.0] * (n + 1)
    acc = 0.0
    for i, v in enumerate(sq):
        acc += v
        pre[i + 1] = acc
    if acc <= 1e-9:
        return None

    # ── 差分函数 d(τ) = Σ(x[j] - x[j+τ])² ────────────────────────
    # 用能量前缀和 + 互相关算，比直接算差分省一半运算。
    d = [0.0] * (max_lag + 1)
    for tau in range(1, max_lag + 1):
        m = n - tau
        num = sum(a * b for a, b in zip(x, x[tau:]))
        d[tau] = pre[m] + (pre[n] - pre[tau]) - 2.0 * num

    # ── 累积均值归一化差分 d'(τ) ─────────────────────────────────
    dp = [1.0] * (max_lag + 1)
    running = 0.0
    for tau in range(1, max_lag + 1):
        running += d[tau]
        if running > 0:
            dp[tau] = d[tau] * tau / running

    # ── 取第一个「过阈值的局部极小」 ──────────────────────────────
    chosen = 0
    for tau in range(min_lag, max_lag + 1):
        if dp[tau] < _YIN_THRESHOLD:
            # 局部极小：右邻存在时不能比右邻大
            if tau + 1 <= max_lag and dp[tau] > dp[tau + 1]:
                continue
            chosen = tau
            break

    if not chosen:
        # 没有过阈值的 → 取 d' 最小处（比自相关稳，仍可能八度错）
        chosen = min(range(min_lag, max_lag + 1), key=lambda t: dp[t])
        if dp[chosen] > 0.6:  # 连最小值都很差 → 不是浊音
            return None

    # ── 抛物线插值取亚采样精度 ───────────────────────────────────
    if 1 < chosen < max_lag:
        a0, a1, a2 = dp[chosen - 1], dp[chosen], dp[chosen + 1]
        denom = a0 - 2 * a1 + a2
        if abs(denom) > 1e-12:
            shift = 0.5 * (a0 - a2) / denom
            if -1.0 < shift < 1.0:
                chosen = chosen + shift

    f0 = _ANALYSIS_RATE / chosen
    if not (_F0_MIN <= f0 <= _F0_MAX):
        return None
    return f0


def median_f0(audio: bytes, ffmpeg_bin: str | None = None) -> float | None:
    """一段音频的中位基频（Hz）。测不出返回 None。"""
    samples = _decode_pcm(audio, ffmpeg_bin)
    if not samples:
        return None
    frame_len = int(_ANALYSIS_RATE * _FRAME_SEC)
    if len(samples) < frame_len:
        return None

    values: list[float] = []
    for start in range(0, len(samples) - frame_len + 1, frame_len):
        f0 = _frame_f0(samples[start : start + frame_len])
        if f0:
            values.append(f0)
    if len(values) < 3:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _probe_rate(path: str, ffmpeg_bin: str | None = None) -> int:
    """探测采样率（asetrate 兜底方案需要）。失败按 24000 处理。

    ⚠️ 这里必须**探准**：采样率算错 → asetrate 的比例错 → 音高位移
    完全跑偏（实测 150Hz 被推到 121Hz，目标 160Hz）。
    之前靠正则切 ffmpeg 的 stderr，结果把 token "Hz" 当成了数字
    （`int("Hz"[:-2])` 直接抛错）→ 静默回退到默认 24000。
    """
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    probe = os.getenv("FFPROBE_BIN", "ffprobe")
    try:
        proc = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        rate = int((proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[0])
        if rate > 0:
            return rate
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
        pass

    # 兜底：从 ffmpeg 的 stderr 里找 "NNNN Hz"
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "info", "-i", path, "-f", "null", "-"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        for line in (proc.stderr or b"").decode("utf-8", "replace").splitlines():
            if "Audio:" not in line:
                continue
            match = re.search(r"(\d+)\s*Hz", line)
            if match:
                return int(match.group(1))
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return 24000


def register_f0(audio: bytes, ffmpeg_bin: str | None = None) -> float | None:
    """一段音频的「音域锚点」= 全部浊音帧基频的 **p25**（稳健分位）。

    ## 为什么用 p25 而不是中位数

    实测（2026-10-22，tertu.cn 的 Qwen3-TTS 真实渲染件）：

        段1  p25=152.1Hz  中位=215.5Hz
        段2  p25=169.2Hz  中位=224.6Hz
        段3  p25=133.2Hz  中位=180.9Hz

    同一段文字合成两次，中位数能从 121 跳到 167（**纯随机采样**），
    而 p25 稳定 —— 因为真实语音的 F0 分布很宽（94~570Hz），
    中位数被上尾带跑，p25 落在「说话人音域的下缘」，那才是身份特征。

    实测对照：同一段 118Hz 的音频，用中位数做度量时位移后测出 304Hz
    （预期 ~188Hz）—— 方向完全错。改用 p25 后，3 段的位移误差 ≤1.8%。
    """
    samples = _decode_pcm(audio, ffmpeg_bin)
    if not samples:
        return None
    frame_len = int(_ANALYSIS_RATE * _FRAME_SEC)
    if len(samples) < frame_len:
        return None

    values: list[float] = []
    for start in range(0, len(samples) - frame_len + 1, frame_len):
        f0 = _frame_f0(samples[start : start + frame_len])
        if f0:
            values.append(f0)
    if len(values) < 10:
        return None
    values.sort()
    return values[int(len(values) * 0.25)]


def _shift_pitch(piece: bytes, semitones: float, ffmpeg_bin: str | None = None) -> bytes | None:
    """把一段音频的音高移动 `semitones` 个半音（**不改变时长**）。失败返回 None。

    ## 两条实现，按可用性降级

    1. `rubberband=pitch=<倍率>` —— 只变调，不动共振峰，质量最好。
       ⚠️ `pitch` 是**频率倍率**不是半音（范围 [0.01, 100]），
       传半音会被 ffmpeg 判越界、整条滤镜失败。
       但**不是所有 ffmpeg 都编了这个滤镜**（实测 macOS homebrew 的
       ffmpeg 8.1.1 没有；生产服务器的系统 ffmpeg 有）。
    2. `asetrate` + `atempo` 兜底 —— 任何 ffmpeg 都有：
       `asetrate=SR·r`（音高 ×r、时长 ÷r）→ `atempo=1/r`（时长还原，
       atempo 变调不变速）。**不能**再插 `aresample=SR` ——
       多这一层会让 mp3 编码器自己的重采样跟它打架，实测输出基频
       偏到 121Hz（目标 160Hz）。

       注：`asetrate` 后的采样率不是 MP3 的标准值，mp3 编码器会
       取最接近的标准值并重采样回去 —— 实测这恰好让**音高偏移保留**
       （150Hz → 160.0Hz，时长 1.995s，采样率 16000），所以不用管。

    两条都失败 → 返回 None，调用方保留原音频（宁可不处理，也不能丢内容）。
    """
    if abs(semitones) < 1e-3:
        return piece
    ffmpeg = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    ratio = 2.0 ** (semitones / 12.0)

    # ⚠️ **保持输入格式**（2026-10-24 补）。
    #
    # 原来输出**恒为 mp3**。但音高对齐现在也被**流水线级拼接**调用
    # （`audio_chunks.align_and_normalize`），而流水线的 WAV 路径靠
    # `b"RIFF"` 判定「所有段都是 WAV」⇒ 若被转成 mp3，判定就假了，
    # 会掉进 ffmpeg concat 兜底，绕开「刻意不依赖 ffmpeg」的纯 Python WAV 拼接。
    is_wav = piece[:4] == b"RIFF"
    codec = ["-c:a", "pcm_s16le"] if is_wav else ["-c:a", "libmp3lame", "-b:a", "64k"]

    with tempfile.TemporaryDirectory(prefix="tts_pitch_") as tmp:
        src = os.path.join(tmp, "in.wav" if is_wav else "in.mp3")
        out = os.path.join(tmp, "out.wav" if is_wav else "out.mp3")
        Path(src).write_bytes(piece)

        # ── 方案 1：rubberband ────────────────────────────────────
        # ⚠️ rubberband 的 `pitch` 是**频率倍率**（ratio），不是半音！
        #    参数范围 [0.01, 100] —— 直接传半音（尤其负的）会被 ffmpeg
        #    判「out of range」而整条滤镜失败，悄悄回落到方案 2。
        #    半音 → 倍率：ratio = 2^(半音/12)
        try:
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
                    f"rubberband=pitch={ratio:.6f}",
                    *codec,
                    out,
                ],
                capture_output=True,
                timeout=300,
                check=True,
            )
            data = Path(out).read_bytes()
            if len(data) > 100:
                return data
        except FileNotFoundError:
            log.warning("TTS 音高对齐失败（无 ffmpeg）")
            return None
        except subprocess.SubprocessError:
            # 多半是「没有 rubberband 滤镜」→ 走兜底
            log.info("TTS 音高对齐：rubberband 不可用，改用 asetrate+atempo")

        # ── 方案 2：asetrate + atempo（通用兜底）──────────────────
        rate = _probe_rate(src, ffmpeg)
        shifted_rate = int(rate * ratio)
        # atempo 只接受 0.5~2.0；我们的位移范围（±2.5 半音 → 0.87~1.15）安全
        tempo = 1.0 / ratio
        try:
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
                    f"asetrate={shifted_rate},atempo={tempo:.6f}",
                    *codec,
                    out,
                ],
                capture_output=True,
                timeout=300,
                check=True,
            )
            data = Path(out).read_bytes()
            if len(data) > 100:
                return data
            log.warning("TTS 音高对齐产出异常(%dB)，保留原音频", len(data))
        except subprocess.SubprocessError as exc:
            log.warning("TTS 音高对齐失败：%s，保留原音频", exc)
    return None


def align_pitch(
    pieces: list[bytes],
    target_f0: float | None = None,
    max_semitones: float = _MAX_SHIFT_SEMITONES,
    deadband_ratio: float = _DEADBAND_RATIO,
    ffmpeg_bin: str | None = None,
) -> tuple[list[bytes], list[dict]]:
    """把各段统一到共同基频。

    Args:
        pieces: 各段音频（顺序即拼接顺序）
        target_f0: 目标基频；None 则取各段基频的**中位数**
        max_semitones: 位移封顶（半音），超过则不动
        deadband_ratio: 死区比例，偏离在它以内不动
        ffmpeg_bin: ffmpeg 路径（默认取环境变量 FFMPEG_BIN）

    Returns:
        (对齐后的各段, 每段的处理明细)
        明细里记着 before/after/半音数 —— 这是**唯一**能证明
        「对齐确实生效」的证据，调用方应该把它记进日志。
    """
    if len(pieces) < 2:
        return pieces, []

    f0s: list[float | None] = [register_f0(p, ffmpeg_bin) for p in pieces]
    known = [v for v in f0s if v]
    if len(known) < 2:
        # 测不出足够多的基频 → 什么都不做（宁可不处理，也别乱处理）
        return pieces, []

    if target_f0 is None:
        # 目标取各段音域锚点的**几何中点**（= 对数域的中点），不是中位数。
        #
        # ## 为什么不是中位数（2026-10-24 实测出来的）
        #
        # 中位数最小化的是「**位移之和**」，但这里存在**封顶**（max_semitones），
        # 于是真正的约束是「**最大位移**」——
        # 一段超封顶就被**放弃不动**，而它继续差一档 ⇒ 换人感还在。
        #
        # 生产实测（art_6d0d007edf3b4bc59def36c9，3 段真实渲染件）：
        #     段1 p25=102.5Hz   段2 p25=117.6Hz   段3 p25=150.1Hz
        #     锚点=中位数 117.6 → 段1 +2.38 ✓（在封顶内）
        #                        段2  0     ✓
        #                        段3 -4.22 ✗ **超封顶 → 被放弃**
        #     ⇒ 对齐后段3 仍差 4.22 半音（26%）⇒ 「换人」没解决
        #     锚点=几何中点 124.0 → 段1 +3.29 ✓  段2 +0.92 ✓  段3 -3.25 ✓
        #                        ⇒ **三段全部落在封顶内，一次都不用放弃**
        #
        # 几何中点（最小频率 × 最大频率 的平方根）正是「最小化最大位移」的锚点：
        # 在对数域里，极端值的中点让两端的位移相等，谁都不超过对方。
        lo, hi = min(known), max(known)
        target_f0 = math.sqrt(lo * hi)
        # ⚠️ 兜底：全篇只有一种音域时 lo==hi ⇒ 锚点=它自己（不产生位移）✓

    out: list[bytes] = []
    details: list[dict] = []
    for piece, f0 in zip(pieces, f0s):
        if not f0:
            out.append(piece)
            details.append({"f0_before": None, "f0_after": None, "semitones": 0.0})
            continue
        ratio = f0 / target_f0
        if abs(ratio - 1.0) <= deadband_ratio:
            out.append(piece)
            details.append({"f0_before": f0, "f0_after": f0, "semitones": 0.0})
            continue
        # ⚠️ 符号：要把 f0 移到 target，需要 target = f0 · 2^(s/12)
        #    → s = 12·log2(target/f0)。写成 log2(f0/target) 会**反向**移动
        #    （实测：对齐后 181.8Hz 被推到 235.3Hz，越对齐越离谱）。
        semitones = 12.0 * math.log2(target_f0 / f0)
        if abs(semitones) > max_semitones:
            # 超出封顶 → 保持原样（人工感比不同人更糟）
            out.append(piece)
            details.append(
                {"f0_before": f0, "f0_after": f0, "semitones": 0.0, "skipped": "over_cap"}
            )
            continue
        shifted = _shift_pitch(piece, semitones, ffmpeg_bin)
        if shifted is None:
            out.append(piece)
            details.append({"f0_before": f0, "f0_after": f0, "semitones": 0.0, "skipped": "ffmpeg"})
            continue
        out.append(shifted)
        details.append({"f0_before": f0, "f0_after": target_f0, "semitones": round(semitones, 2)})

    return out, details
