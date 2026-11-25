#!/usr/bin/env python3
"""exp_tts_identity_control.py —— 音色身份审计的**对照组**素材。

## 为什么要有这个脚本（2026-11-25）

`exp_tts_identity_audit.py` 采集的是「同一份参考音」的克隆段，
量的是**同一人带**。要判断「某个余弦算不算换人」，还缺一条**换人带**。

音色库里只有 1 个 active 音色（婷婷）⇒ 拿不到「另一个音色」的克隆。
⇒ 改用上游的**预设音色**（不走克隆，直接指定 `voice=`）：
上游枚举 `aiden, dylan, eric, ono_anna, ryan, serena, sohee, uncle_fu, vivian`。

⚠️ 这条对照量的严格说是「上游自带音色 vs 我们克隆的音色」，
    两者的**音色来源不同**（一个预设 speaker embedding，一个参考音克隆），
    所以它给出的是「换人带的上界」而不是精确的换人余弦。
    ⇒ 判读时它是**下界参照**：如果某个克隆段与它的余弦都高于这条带，
      那就绝对不可能是「换人」。

## 采集什么

    control/vivian.mp3   上游预设音色 vivian 念同一段文本（无克隆）
    control/serena.mp3   同上
    control/uncle_fu.mp3 同上（与生产 voice=uncle_fu 一致 —— 注意它**不是**克隆）

用法（生产）：

    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_identity_control.py --text-file /tmp/exp_script.txt

退出码：0 = 采齐；2 = 跑不起来。
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

# 上游枚举的预设音色（见 openai.py 的表）
_CONTROL_VOICES = ("vivian", "serena", "uncle_fu")


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks
    from stashbox.backend.app.services.tts.voice_align import f0_profile

    text = Path(args.text_file).read_text(encoding="utf-8").strip() if args.text_file else ""
    if not text:
        print("❌ 需要 --text-file")
        return 2

    client = await tts_reload()
    chunk_chars = int(getattr(client, "clone_chunk_chars", 800) or 800)
    chunks = split_into_chunks(text, chunk_chars)
    probe = chunks[0]
    print(f"[客户端] provider={client.provider_name}  探针 {len(probe)} 字", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for voice in _CONTROL_VOICES:
        try:
            audio = await client._synthesize_chunk(probe, voice=voice, clone=None)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {voice}: {type(exc).__name__}: {exc}", flush=True)
            continue
        path = out_dir / f"{voice}.mp3"
        path.write_bytes(audio)
        prof = f0_profile(audio)
        print(
            f"  ✓ {voice}: {len(audio)} bytes  p25={prof.p25} median={prof.median}",
            flush=True,
        )
    print(f"\n✅ 对照组素材 → {out_dir}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="音色身份审计对照组（上游预设音色）")
    ap.add_argument("--text-file", required=True, help="文案文件（取第一块当探针）")
    ap.add_argument("--out-dir", default="/tmp/tts_identity/control", help="输出目录")
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
