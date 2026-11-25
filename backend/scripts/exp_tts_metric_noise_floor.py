#!/usr/bin/env python3
"""exp_tts_metric_noise_floor.py —— 测「段间散布」这个口径的**噪声底**。

## 为什么要有这个脚本（2026-11-25）

上下文续写的 A/B 对比实验（`exp_tts_contextual_continuation.py`）跑出：

    A 现状：段间散布 44.1%，最大位移 2.41 半音
    B 续写：段间散布 50.8%，最大位移 2.98 半音

⇒ 看似「B 略差」⇒ 上下文续写没收益。

**但同一份文案、同样的配置，A 组换个时间再跑一次，散布变成了 13.4%、位移 0.73 半音**
⇒ **这个口径的重复测量方差极大**（差 3 倍）⇒ A/B 那 6.7 个百分点的差异
**在噪声里，不构成证据**。

## 怎么判「这个口径能不能用」

测**噪声底**：**同一块文本**、同一份参考音，渲染 N 次，量每次的 p25 散布。

- 若噪声底 ≈ 段间散布 ⇒ 散布这个量**主要在测噪声**，不能用来比较方案；
- 若噪声底 ≪ 段间散布 ⇒ 散布能区分「换没换人」，A/B 比较才有效。

为什么这是唯一干净的对照：**同一段文本** ⇒ 文案差异被消掉
（文案差异实测可达 13%，比判据本身还大 —— 见 `check_tts_voice_consistency.py`）
剩下的差异才是「上游每次采样飘了多少」。

用法（生产）：
    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_metric_noise_floor.py

退出码：0 = 测出来了（看 stdout）；2 = 跑不起来。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()


async def _default_voice() -> tuple[str, str, str]:
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=False)
    default = next((v for v in voices if v.is_default), None)
    if default is None:
        raise SystemExit("音色库没有默认音色 —— 没法做这个实验")
    return default.ref_audio_url, default.ref_text, default.display_name


def _spread(values: list[float | None]) -> float:
    vals = [v for v in values if v]
    if len(vals) < 2:
        return 0.0
    lo = min(vals)
    return (max(vals) - lo) / lo if lo > 0 else 0.0


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks
    from stashbox.backend.app.services.tts.voice_align import f0_profile

    text = Path(args.text_file).read_text(encoding="utf-8").strip() if args.text_file else ""
    ref_audio, ref_text, name = await _default_voice()
    print(f"[音色] 音色库默认音色：{name}")

    client = await tts_reload()
    chunk_chars = int(getattr(client, "clone_chunk_chars", 800) or 800)

    if text:
        chunks = split_into_chunks(text, chunk_chars)
        probe = chunks[0]
    else:
        probe = args.probe_text
    print(f"[探针] {len(probe)} 字（块长上限 {chunk_chars}）")

    clone = await client._resolve_clone(ref_audio, ref_text)
    if clone is None:
        raise SystemExit("拿不到克隆参数")

    p25s: list[float | None] = []
    meds: list[float | None] = []
    for i in range(args.repeat):
        audio = await client._synthesize_chunk(probe, voice=client.voice, clone=clone)
        prof = f0_profile(audio)
        p25s.append(prof.p25)
        meds.append(prof.median)
        print(
            f"  第 {i + 1} 次渲染：p25={prof.p25 if prof.p25 else '—'}Hz  "
            f"median={prof.median if prof.median else '—'}Hz  帧数={prof.frame_count}"
        )

    print()
    print(f"⇒ 同文重渲 p25 散布（**噪声底**）：{_spread(p25s) * 100:.1f}%")
    print(f"⇒ 同文重渲 median 散布：{_spread(meds) * 100:.1f}%")
    print()
    print("判读：")
    print("  · 噪声底 ≫ 5%（判据阈值）⇒ 「散布」这个量主要在测噪声，")
    print("    不能用来比较方案 ⇒ 上游独立采样本身就飘得很厉害，")
    print("    而事后移调对齐是在**追一个追不上的目标**。")
    print("  · 噪声底很小（<5%）⇒ 散布能区分「换没换人」，A/B 比较有效。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="测「段间散布」口径的噪声底")
    ap.add_argument("--text-file", help="从文件取文案（取第一块当探针）")
    ap.add_argument("--repeat", type=int, default=3, help="渲染几次（默认 3）")
    ap.add_argument("--probe-text", default="", help="自定义探针文本")
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
