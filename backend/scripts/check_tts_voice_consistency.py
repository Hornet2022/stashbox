#!/usr/bin/env python3
"""check_tts_voice_consistency.py — 实测「分段音色一致性」。

## 为什么要有这个脚本（2026-10-24 内测反馈）

内测用户反馈「历史音频存在分段音色差异」。根因实测闭合：

    托管端点（tertu.cn / Qwen3-TTS-12Hz-1.7B-Base）**不认 `voice` 这个说话人名**
    —— 同一段文本、同一个 `voice=uncle_fu` 连发 3 次，基频中位数
    165.0 / 188.5 / 105.3 Hz（三个不同的人）。

    我们按 700 字分块 → 每块一次请求 → **每块一个人**。

修法是让零样本克隆真的生效（同份 ref_audio 连发 3 次 → 217.9 / 219.3 / 224.7 Hz）。
但「代码看起来对」不等于「音色真的一致」—— 这个脚本用**真实上游**去验。

## 判据

    各块基频中位数，相对全篇中位数的最大偏差 < 5%

⚠️ **这个 5% 是校准过的，不是拍脑袋**（实测 2026-10-24）：

    同文连发 3 次的噪声底（同一段文本、同一份参考音）：
        7s 短文本  → 散布 7.8%   ← 太短，是**度量噪声**不是音色漂移
        65s 块长   → 散布 1.6%   ← 生产块长（700 字 ≈ 65s 音频）

    修好之前（随机音色）块间差异是 **30~50%**；修好之后是 **1~3%**。
    5% ≈ 3 倍噪声底，既有区分度，又不会被上游的正常抖动误伤。

    ⚠️ 度量用的是 `app/services/tts/voice_align.py:median_f0`（YIN 估计，纯 Python）：
       自相关法会犯**八度错误**（真实 170Hz 报成 85Hz），用它当判据等于没有判据。

## 用法

    # 用音色库里的默认音色跑（生产巡检）
    python scripts/check_tts_voice_consistency.py

    # 指定文案（默认是内置的 ~2100 字长文，会切成 3+ 块）
    python scripts/check_tts_voice_consistency.py --text-file /path/to/script.txt

    # 对照组：不带克隆（复现「每块一个人」的旧行为，用来确认这个脚本真的能测出来）
    python scripts/check_tts_voice_consistency.py --no-clone

    # 只看噪声底：同一段文本连发 3 次
    python scripts/check_tts_voice_consistency.py --baseline-only

退出码：0 = 通过；1 = 不通过（音色不一致）；2 = 跑不起来（配置/音色库/上游问题）。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

# ── 包路径自举 ─────────────────────────────────────────────────────────
# 代码里全是 `stashbox.backend.*` 这种绝对导入，而 `stashbox` 是**仓库目录名**
# （不是顶层包）—— 所以 sys.path 里要放的是**仓库根的父目录**。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
# 复用同目录脚本的 env 加载（同一份实现，别再抄一遍）
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

# 判据：各块基频中位数相对全篇中位数的最大相对偏差
MAX_SPREAD = 0.05

# 默认长文（~2100 字 → 700 字分块 → 3 块）。
#
# ⚠️ 必须是**内容不同**的多段：拿同一段重复 N 次，块间基频会一模一样，
# 测出来的散布是 0 —— 那是文本的复读，不是音色的一致性。
DEFAULT_TEXT = """
碎片时间到底能不能学得进去，这是个被问烂了的问题。我的回答是能，但要先接受一件事：碎片时间的价值不在于完成一个完整的目标，而在于让大脑持续保持在被唤醒的状态。你在通勤路上听完一篇深度文章，在午休前读三个行业观点，这些东西单独看都很小，但它们共同维持着一种对世界的敏感度。

很多人把碎片时间当成敌人，觉得它是注意力被切碎的罪魁祸首。但真正毁掉深度阅读的，从来不是时间被切碎，而是**期待自己在碎片里完成深度阅读**。你带着「我要读完一章书」的心愿挤进三分钟的地铁电梯间，然后责怪自己读不进去——问题不在那三分钟，在那个期待。

真正稀缺的不是注意力，是判断力。一条内容值不值得读，在打开它之后的三十秒内基本就能判断出来。我的做法是先看标题和前三段，如果觉得有料再往下看，觉得只是自我安慰就立刻划走。判断错了的成本，远远低于读完一篇废话的成本——后者要花你二十分钟，还可能留下「我今天学到了什么」的错觉。

知识的复利也不在单次学得多深，而在不同来源之间的连接。同一件事，你从三个完全不同的角度看过去，理解会变成一个立体的东西，这时候再回头看，你会发现自己的判断已经变了。碎片时间的优势恰恰在这里：它天然逼着你从多个来源取材，而不是在一棵树上吊死。

当然也有反面。碎片输入很容易变成一种被动消费，像刷feed一样，看似接触了很多信息，实际上一条都没进去。判断标准很简单：如果你能用自己的话复述刚才看到的东西，那它就是输入；如果只是觉得「好像有道理」，那它只是噪音。

所以我的建议是：把碎片时间当成积木，而不是当成积木的替代品。单独看每一块都很小，但只要搭的方向对，积着积着就会成形。真正决定形状的，不是你每天有多少碎片，而是你有没有一块都没放错位置。
""".strip()


# ── 工具 ───────────────────────────────────────────────────────────────
def _fmt_hz(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}Hz"


def measure(pieces: list[bytes]) -> list[float | None]:
    """逐块测基频中位数。"""
    from stashbox.backend.app.services.tts.voice_align import median_f0

    return [median_f0(p) for p in pieces]


def spread(values: list[float]) -> tuple[float, float]:
    """返回 (相对整体中位数的最大偏差, 极差/中位数)。

    用「相对**整体中位数**」而不是「相对块 1」：
    块 1 自己也带噪声，拿它当基准会把误差放大一倍。
    """
    med = statistics.median(values)
    max_dev = max(abs(v - med) / med for v in values)
    rng = (max(values) - min(values)) / med
    return max_dev, rng


async def _capture_chunks(client, text: str, ref_audio: str | None, ref_text: str | None):
    """跑一次真实的分块合成，并把**逐块**音频抓出来。

    怎么抓的：钩住 `openai` 模块里的 `concat_audio` —— 它拿到的就是
    「每一块渲染出来的原始音频」。这比在脚本里自己切一遍要准：
    走的是**生产同一条路径**（工厂建 client → 配置 → 分块 → 逐块请求）。

    ⚠️ 二分自愈会**先**调 concat（子块），再由外层调一次 ——
       所以取**最后一次**调用（外层那次才是整篇的分块结果）。
    """
    from stashbox.backend.app.services.tts import openai as openai_module

    calls: list[list[bytes]] = []
    original = openai_module.concat_audio

    def _spy(pieces, gap_sec: float = 0.3):
        calls.append(list(pieces))
        return original(pieces, gap_sec=gap_sec)

    openai_module.concat_audio = _spy
    try:
        audio = await client.synthesize(
            text,
            voice=getattr(client, "voice", None),
            ref_audio=ref_audio,
            ref_text=ref_text,
        )
    finally:
        openai_module.concat_audio = original

    return audio, (calls[-1] if calls else [audio])


# ── 主流程 ─────────────────────────────────────────────────────────────
async def _resolve_voice() -> tuple[str | None, str | None, str]:
    """取要用的克隆音源：音色库默认音色 → （没有就报错并给出修法）。"""
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=False)
    default = next((v for v in voices if v.is_default), None)
    if default is None:
        raise SystemExit(
            "音色库没有默认音色 —— 克隆没有音源可用。\n"
            "  生产上先建一条：上传参考音频 + 参考文本，并设为默认。\n"
            "  （在此之前，蒸馏会回落到全局配置，用的是上游随机音色。）"
        )
    return default.ref_audio_url, default.ref_text, default.display_name


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    else:
        text = DEFAULT_TEXT

    ref_audio = ref_text = None
    voice_name = ""
    if not args.no_clone:
        ref_audio, ref_text, voice_name = await _resolve_voice()
        print(f"[音色] 音色库默认音色：{voice_name}（参考音频 {ref_audio[:60]}…）")
    else:
        print("[音色] 对照组：不带克隆（复现「每块一个人」的旧行为）")

    client = await tts_reload()
    print(
        f"[客户端] provider={client.provider_name} 分块上限={getattr(client, 'chunk_chars', '?')} 字"
    )

    # ── 噪声底：同一段文本连发 3 次 ────────────────────────────────
    if args.baseline_only or not args.skip_baseline:
        probe_text = text[: getattr(client, "chunk_chars", 700)]
        same: list[float | None] = []
        for i in range(3):
            _, pieces = await _capture_chunks(client, probe_text, ref_audio, ref_text)
            same.append(measure(pieces)[0])
        vals = [v for v in same if v]
        if len(vals) >= 2:
            dev, rng = spread(vals)
            print(
                f"[噪声底] 同一段文本连发 3 次："
                f"{' / '.join(_fmt_hz(v) for v in same)}"
                f" → 最大偏差 {dev * 100:.1f}%（极差 {rng * 100:.1f}%）"
            )
        else:
            print("[噪声底] 测不出基频（音频太短或 ffmpeg 不可用）")

    # ── 主体：整篇分块合成，看块间一致性 ───────────────────────────
    audio, pieces = await _capture_chunks(client, text, ref_audio, ref_text)
    f0s = measure(pieces)
    print(f"\n[整篇] {len(text)} 字 → {len(pieces)} 块，合计 {len(audio)} 字节")
    for i, (p, f0) in enumerate(zip(pieces, f0s), 1):
        print(f"  块{i}: {len(p):>8d}B  基频中位数 {_fmt_hz(f0)}")

    vals = [v for v in f0s if v]
    if len(vals) < 2:
        print("\n❌ 测不出足够的基频样本，无法判定")
        return 2

    dev, rng = spread(vals)
    print(f"\n全篇基频中位数 = {_fmt_hz(statistics.median(vals))}")
    print(
        f"块间最大偏差 = {dev * 100:.1f}%（极差 {rng * 100:.1f}%）  判据 < {MAX_SPREAD * 100:.0f}%"
    )

    if dev < MAX_SPREAD:
        print("\n✅ 通过：各块是同一个音色")
        return 0
    print(
        "\n❌ 不通过：块间音色不一致（用户听到的是好几个人在念）\n"
        "   排查顺序：\n"
        "     1. 日志里每段是不是都带了 ref_audio（clone=on）\n"
        "     2. 音色库的参考音频是不是同一份、每段是否逐字节相同\n"
        "     3. 参考文本是否与参考音频内容一致"
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="实测分段音色一致性")
    ap.add_argument("--text-file", help="自定义文案（默认内置长文）")
    ap.add_argument("--no-clone", action="store_true", help="对照组：不带克隆")
    ap.add_argument("--baseline-only", action="store_true", help="只测噪声底")
    ap.add_argument("--skip-baseline", action="store_true", help="跳过噪声底测量")
    args = ap.parse_args()
    try:
        return asyncio.run(_run(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 跑不起来: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
