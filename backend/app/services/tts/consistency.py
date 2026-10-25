"""音色一致性判据 —— 三层，各自有阈值与标定依据（2026-10-25）。

## 为什么要有这个模块

「一篇音频听出好几种音色」这件事上，各方说法长期不一致：
上游到底会不会换人？分段是不是元凶？基频漂移算不算音色？
**每个说法都缺一个可复算的判据**，于是每次讨论都在互相推翻。

这个模块把判据**缝成一份**：每一层写清楚「量什么、阈值多少、依据是什么」，
并把**测量工具的口径**也固定下来。

## 三层判据（注意：它们量的不是同一件事）

### ① 身份层：是不是同一个人（**金标准，离线**）

用 ECAPA-TDNN（业界标准的说话人嵌入）算整段余弦相似度。

⚠️ **这一层不能在生产运行时跑** —— ECAPA 需要 torch + speechbrain，
生产 venv 装不了（生产只有 numpy/ffmpeg）。它属于**离线审计**：
在有 torch 的机器上跑 `scripts/check_tts_timbre_ecapa.py`，
用标定好的分离带判断「块与块之间是不是同一个人」。

标定（2026-10-25，同一上游 tertu.cn / Qwen3-TTS-12Hz-1.7B-CustomVoice，
克隆源 = 婷婷）：

    同一人（克隆段 / 同音色另一次渲染）  整段余弦 0.75 ~ 0.80
    同一段文本重渲一次                   整段余弦 0.93 ~ 0.95
    换人（vivian / serena 等预设音色）   整段余弦 0.29 ~ 0.35

⇒ 分离带很宽：**换人 ≤ 0.35，同一人 ≥ 0.75**，门禁阈值取中点 **0.55**。

⚠️ **绝对阈值依实现而变**（F5-TTS 模型卡的原话：ECAPA/CAM++ 的阈值
必须在自己的数据上标定）。换模型、换上游、换 ECAPA 版本 ⇒ **重新标定**。

### ② 韵律层：音域一不一致（**运行时可算**）

量各段基频中位数的散布（`voice_align.f0_profile`，YIN 估计）。

⚠️ **跨段比较基频量到的是「文案差异」，不是「换没换人」** ——
实测同一篇稿子切成 4 块，块间基频差 17.8%；而同一段文本重渲 3 次只差 2.4%。
所以这一层**只作参考**，**不作身份门禁**（这条结论已写进
`scripts/check_tts_voice_consistency.py` 的 docstring，别再推翻它）。

### ③ 电平层：段与段之间有没有音量跳变（**运行时可算**）

段间音量跳变会被听成「换人了」。这一层与文案无关，是**可门禁**的。

## 结构性判据：段数 = 接缝数

**接缝就是「一篇听出好几种音色」的来源** —— 分段 = 多次独立采样。
生产实测（2026-10-25）：2544 字的稿子切成 **12 段**（11 个接缝）。
⇒ 段数是**最直接、最可测**的一致性指标，见 `MAX_SEGMENTS`。

## 已知局限（诚实记录）

· 速率判据（`openai._TRUNCATION_CHARS_PER_SEC`）只能筛「缺得很狠」的截断，
  轻微截尾抓不到 —— 详见那里注释里的标定表。
· 韵律层的跨段散布**不能**当身份判据（见上）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── ① 身份层（金标准，离线）──────────────────────────────────────────────
#
# 标定（2026-10-25，ECAPA-TDNN spkrec-ecapa-voxceleb，整段余弦）：
#     同一人（克隆段 / 同音色另一次渲染）  0.75 ~ 0.80
#     同一段文本重渲一次                  0.93 ~ 0.95
#     换人（预设音色 vivian / serena）    0.29 ~ 0.35
#
# ⇒ 门禁阈值取分离带中点，两侧都有 0.2 以上的余量。
ECAPA_SAME_MIN = 0.75
ECAPA_DIFF_MAX = 0.35
ECAPA_THRESHOLD = 0.55

# ── ② 韵律层（运行时可算，仅参考）────────────────────────────────────────
#
# 同一段文本重渲的基频散布（实测 2.2% ~ 2.9%），
# 换人的散布实测 26% ~ 34% ⇒ 5% 落在两者之间。
#
# ⚠️ **只适用于「同一段文本的多次渲染」** —— 跨段（不同文本）比基频
#    量到的是文案差异（实测可达 17.8%），不能当身份判据。
PITCH_SPREAD_MAX = 0.05

# ── ③ 电平层（运行时可算，可门禁）────────────────────────────────────────
#
# 实测（2026-10-25，生产一篇文章的 7 个克隆段）：段间 RMS 散布 **0.05 dB**
# ⇒ 1.0 dB 的门槛对当前上游极其宽松（永远不会被触发）；
# 它的作用是「上游哪天开始给段与段不同的电平时，这里会响」。
LOUDNESS_SPREAD_MAX_DB = 1.0

# ── 结构性判据：段数 ─────────────────────────────────────────────────────
#
# 生产实测：2544 字的稿子 → 12 段（老块长 250 字）；
# 块长放大到 800 字后 → 4 段。
# ⇒ 接缝数 = 段数 - 1。超过 4 段就说明块长配置没生效。
MAX_SEGMENTS = 4


@dataclass
class ConsistencyReport:
    """一篇文章（若干段）的音色一致性报告。"""

    segments: int
    seams: int
    loudness_spread_db: float | None = None
    pitch_spread: float | None = None
    pitch_medians: list[float] = field(default_factory=list)
    loudness_levels: list[float] = field(default_factory=list)
    comparable_pitch: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def judge_article(pieces: list[bytes]) -> ConsistencyReport:
    """量一篇文章（逐段音频）的音色一致性并给出判定。

    只做**运行时可算**的两件事：电平层 + 段数；
    韵律层顺带量出来但**只作参考**（跨段比基频量的是文案差异）。

    量不到（ffmpeg 不可用 / 片段太短）→ 对应项留空，
    **不判失败**：「不可判」不等于「不一致」。
    """
    from .audio_chunks import _measure_piece_db
    from .voice_align import MIN_FRAMES_FOR_COMPARISON, f0_profile

    report = ConsistencyReport(segments=len(pieces), seams=max(0, len(pieces) - 1))

    levels: list[float] = []
    for piece in pieces:
        value = _measure_piece_db(piece)
        levels.append(value if value is not None else float("nan"))
    known = [v for v in levels if v == v]
    report.loudness_levels = known
    if len(known) >= 2:
        report.loudness_spread_db = max(known) - min(known)

    medians: list[float] = []
    comparable = 0
    for piece in pieces:
        profile = f0_profile(piece)
        if profile.median:
            medians.append(profile.median)
        if profile.frame_count >= MIN_FRAMES_FOR_COMPARISON and profile.median:
            comparable += 1
    report.pitch_medians = medians
    if len(medians) >= 2:
        ordered = sorted(medians)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        if median:
            report.pitch_spread = (ordered[-1] - ordered[0]) / median
    report.comparable_pitch = comparable >= 2

    if report.segments > MAX_SEGMENTS:
        report.problems.append(
            f"段数 {report.segments} > {MAX_SEGMENTS}（接缝 {report.seams} 个）—— "
            f"块长配置可能没生效，接缝是「听出好几种音色」的直接来源"
        )
    if report.loudness_spread_db is not None and report.loudness_spread_db > LOUDNESS_SPREAD_MAX_DB:
        report.problems.append(
            f"段间响度差 {report.loudness_spread_db:.2f} dB > "
            f"{LOUDNESS_SPREAD_MAX_DB} dB —— 音量跳变会被听成「换人了」"
        )

    return report


def format_report(report: ConsistencyReport) -> str:
    """把报告渲染成人读的文本。"""
    lines = [
        f"段数 {report.segments}（接缝 {report.seams}）",
    ]
    if report.loudness_levels:
        levels = ", ".join(f"{v:.1f}" for v in report.loudness_levels)
        lines.append(f"各段 RMS(dBFS): {levels}")
    if report.loudness_spread_db is not None:
        lines.append(f"响度散布: {report.loudness_spread_db:.2f} dB")
    if report.pitch_medians:
        medians = ", ".join(f"{v:.1f}" for v in report.pitch_medians)
        lines.append(f"各段基频中位数(Hz): {medians}")
    if report.pitch_spread is not None:
        note = "" if report.comparable_pitch else "（片段偏短，统计量仅供参考）"
        lines.append(f"基频散布: {report.pitch_spread * 100:.1f}% {note}（仅参考，不作门禁）")
    if report.problems:
        lines.append("判定: ❌ 不通过")
        lines.extend(f"  · {p}" for p in report.problems)
    else:
        lines.append("判定: ✅ 通过")
    return "\n".join(lines)
