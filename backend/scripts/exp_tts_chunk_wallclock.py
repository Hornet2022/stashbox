#!/usr/bin/env python3
"""exp_tts_chunk_wallclock.py —— 上游**单次请求能吃多长**（接缝数的上限）。

## 为什么要有这个脚本（2026-11-25）

「一篇听出好几种音色」的**结构性**来源是**接缝**（`consistency.py`：
段数 = 接缝数 = 独立采样次数）。生产实测一篇文章切成 2~4 块 ⇒ 1~3 个接缝。

⇒ 减少接缝有两条路：
    ① 把块放大（块长上限 = 上游单次请求能吃多长）
    ② 让每次采样都锚在上一次（上下文续写）

① 的天花板由**上游墙钟**决定。仓里记的是 2026-10-24 在 **旧上游**（tertu.cn）
实测的 60s 墙钟：
    400 字 → wall 24.6s   语速 4.5 字/秒
    700 字 → wall 42.5s
⇒ 60s / 0.0607 ≈ **990 字** ≈ 现在的 800 字（已接近上限）。

但**上游已经换过**（现在是 hub.tsingfly.com / Qwen3-TTS-12Hz-1.7B-CustomVoice），
旧上限未必还成立 ⇒ 必须**重新测**。

⇒ 如果上游其实允许 300s 墙钟 ⇒ 整篇一次合成（2500~3000 字）可行 ⇒
  **接缝数直接归零**，「分段音色不一致」这个病根就没了。

用法（生产）：

    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_chunk_wallclock.py --text-file /tmp/exp_script.txt

退出码：0 = 测完（看 stdout 表格）；2 = 跑不起来。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
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


async def _default_voice():
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=False)
    default = next((v for v in voices if v.is_default), None)
    if default is None:
        raise SystemExit("音色库没有默认音色")
    return default


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.voice_align import f0_profile

    text = Path(args.text_file).read_text(encoding="utf-8").strip()
    voice = await _default_voice()
    client = await tts_reload()
    clone = await client._resolve_clone(voice.ref_audio_url, voice.ref_text)
    if clone is None:
        raise SystemExit("拿不到克隆参数")

    print(f"[音色] {voice.display_name}   [上游] {client.base_url}  model={client.model}")
    print(f"[客户端] httpx timeout={getattr(client, 'timeout', '?')}s")
    print(f"\n{'字数':>7}{'墙钟s':>10}{'字/秒':>9}{'音频s':>9}{'秒/字':>10}{'字节':>10}   结果")
    print("-" * 78)

    rate_ref = None  # 音频秒/字 的基线（第一个成功尺寸）
    results = []
    for size in args.sizes:
        probe = text[:size]
        if len(probe) < size:
            print(f"（文案只有 {len(text)} 字，跳过 {size}）")
            continue
        t0 = time.time()
        try:
            audio = await client._synthesize_chunk(probe, voice=client.voice, clone=clone)
        except Exception as exc:  # noqa: BLE001
            wall = time.time() - t0
            print(
                f"{size:>7}{wall:>10.1f}{'-':>9}{'-':>9}{'-':>10}   ❌ {type(exc).__name__}: {exc}"
            )
            results.append((size, wall, None, None, False))
            continue
        wall = time.time() - t0
        prof = f0_profile(audio)
        dur = prof.duration_sec
        # 音频秒/字 —— 用来抓**静默截断**。
        # 字数越多，这个比值应当**稳定**；一旦某个尺寸明显偏高 ⇒ 那段音频
        # 比应有的短 ⇒ 上游把它截断了（HTTP 200 + 合法音频，内容少了）。
        sec_per_char = dur / size if size else 0.0
        flag = ""
        if rate_ref and sec_per_char > rate_ref * 1.15:
            flag = f"  ⚠️ 秒/字 {sec_per_char:.3f} > 基线 {rate_ref:.3f} ⇒ **疑似截断**"
        print(
            f"{size:>7}{wall:>10.1f}{size / wall:>9.2f}{dur:>9.1f}"
            f"{sec_per_char:>10.3f}{len(audio):>10}   ✅{flag}"
        )
        results.append((size, wall, dur, len(audio), True))
        if rate_ref is None:
            rate_ref = sec_per_char

    print()
    ok = [r for r in results if r[4]]
    if ok:
        biggest = max(ok, key=lambda r: r[0])
        print(
            f"⇒ 单次请求**实测可行**的最大字数：{biggest[0]} 字"
            f"（墙钟 {biggest[1]:.1f}s，音频 {biggest[2]:.1f}s）"
        )
        print("⇒ 现在 _CLONE_CHUNK_CHARS_DEFAULT = 800")
        n_now = (len(text) + 799) // 800
        n_max = (len(text) + biggest[0] - 1) // biggest[0]
        print(
            f"⇒ {len(text)} 字的生产稿：块长 800 → {n_now} 块（接缝 {n_now - 1}）；"
            f"块长 {biggest[0]} → {n_max} 块（接缝 {n_max - 1}）"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="上游单次请求的字数上限（墙钟）")
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--sizes", type=int, nargs="+", default=[800, 1000, 1200, 1500, 2000, 2600])
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
