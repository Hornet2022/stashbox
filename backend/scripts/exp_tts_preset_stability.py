#!/usr/bin/env python3
"""exp_tts_preset_stability.py —— 「固化音色」候选方案的决定性实验（采集端）。

## 背景（2026-11-25）

用户拍板：**固化音色根治，存量内容不动**。

存量核验（`scripts/tts_stock_audit_2026-11-25/`）证明：零样本克隆路径
（逐块透传同一份 ref_audio）**不是确定性固定说话人**——首批 18 篇重生成
（全部 clone=on、2 块）里 2 篇接缝抽到明显不同的人（滑窗 min 0.2427 / 0.6662，
≈12% 接缝失败率）。块长 800→1500 只减少接缝数，不归零失败率。

仓内既有证据指向一条现成的根治路径——**上游预设音色**：

* 契约 §1.3（旧上游 tertu.cn）：`voice=uncle_fu`（无 ref）连渲 3 次，
  ECAPA 互相 0.93~0.95 = 同一个人；「同一 voice 连发 3 次 = 3 个人」
  已被否证（那是基频推断，不是身份层）。
* 当前上游（hub.tsingfly.com）实测：`control/uncle_fu.mp3` vs 婷婷克隆源
  整段余弦 **0.8849**，落在同一人带（0.857~0.931）⇒ **预设 uncle_fu
  就是婷婷的音色身份**。换预设模式不改变「谁在念」。

但**旧上游的 3 次稳定性不能直接外推到当前上游**，且 n=3 太小
（克隆路径在小样本里也显得稳定，生产才暴露 12% 失败）。
⇒ 本实验在**当前上游**用**生产真实稿 + 生产块长**采一批预设音色渲染件，
离线量身份层，回答：**预设模式跨请求是不是固定说话人？**

## 预注册判据（判定端 exp_tts_preset_stability_judge.py，阈值 import 仓内 consistency.py）

设采到 K 块（不同文本）+ 2 块同文重渲：

1. **整段全对余弦**：所有块两两 ≥ ECAPA_SAME_MIN(0.85)；
   任何一对 ≤ ECAPA_DIFF_MAX(0.70) = 出现换人 ⇒ 方案否证。
2. **接缝滑窗**（6s 窗 / 2s 步，相邻块硬拼）：每条缝 min ≥ 0.82
   （滑窗同一人下界，契约 §2.4.1 单独标定）；任何窗口对 ≤ 0.70 = 真换人。
3. **同文重渲噪声底**：两两 ≥ 0.93（旧上游预设 3 连渲的水平）作参照。

克隆路径的已知失败率 ≈ 12%/接缝。若预设模式真有同样失败率，
K=10 块（9 条缝 + 45 个整段对）全过的概率 ≈ 0.88^9 ≈ 32% ——
所以「全过」还要看**分布形态**：预设若固定说话人，整段对应该紧凑地
落在同人带（类比克隆同人带 0.931~0.978），而不是偶尔掉到 0.7 以下。

## 采什么

    P01.mp3 ... Pnn.mp3   预设音色（voice=配置值，无 ref_audio）逐块渲染
    R1.mp3 / R2.mp3       第一块同文重渲（噪声底）
    manifest.json         每块文本字数 / 墙钟 / 字节数 / 来源 article_id

文本来源：生产 DB 最近 4 篇 done 且 1800~3500 字的真实稿（各不同），
用生产切块逻辑 split_into_chars 切块 ⇒ 与真实蒸馏同形态。

## 用法（生产）

    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_preset_stability.py \
        --out-dir /tmp/tts_preset_stability --chunk-chars 1500 --delay 15

退出码：0 = 采齐（或有失败但已记录）；2 = 跑不起来。
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def _fetch_scripts(n: int) -> list[dict]:
    """生产真实稿：最近 n 篇 done、1800~3500 字、互不相同。"""
    from sqlalchemy import text

    from stashbox.backend.common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT article_id, script_text FROM distilled_articles "
                        "WHERE status='done' AND script_text IS NOT NULL "
                        "AND length(script_text) BETWEEN 1800 AND 3500 "
                        "ORDER BY created_at DESC LIMIT :n"
                    ),
                    {"n": n},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks

    client = await tts_reload()
    voice = getattr(client, "voice", None)
    print(
        f"[客户端] provider={client.provider_name} model={getattr(client,'model',None)} "
        f"base_url={getattr(client,'base_url',None)} voice={voice}",
        flush=True,
    )
    if client.provider_name != "openai":
        print("❌ 生产 provider 不是 openai，实验前提不成立")
        return 2

    if args.text_file:
        scripts = [
            {
                "article_id": "text-file",
                "script_text": Path(args.text_file).read_text(encoding="utf-8"),
            }
        ]
    else:
        scripts = await _fetch_scripts(args.scripts)
    if not scripts:
        print("❌ 没取到生产稿")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    blocks: list[tuple[str, str, str]] = []  # (label, article_id, text)
    for sc in scripts:
        chunks = split_into_chunks(sc["script_text"], args.chunk_chars)
        for i, ch in enumerate(chunks):
            blocks.append((f"P{len(blocks)+1:02d}", sc["article_id"], ch))
    # 同文重渲噪声底：第一块再渲 2 次
    if blocks:
        blocks.append(("R1", blocks[0][1], blocks[0][2]))
        blocks.append(("R2", blocks[0][1], blocks[0][2]))

    print(
        f"[计划] {len(scripts)} 篇稿 → {len(blocks)} 次请求（含 2 次同文重渲），块长≤{args.chunk_chars}",
        flush=True,
    )

    failures = 0
    for idx, (label, aid, text) in enumerate(blocks):
        if idx:
            await asyncio.sleep(args.delay)
        wall = None
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                # ⚠️ clone=None ⇒ 预设音色路径（上游按 voice 名取固定说话人）
                audio = await client._synthesize_chunk(text, voice=voice, clone=None)
                wall = time.monotonic() - t0
                break
            except Exception as exc:  # noqa: BLE001
                wall = time.monotonic() - t0
                print(
                    f"  ✗ {label} 尝试{attempt}: {type(exc).__name__}: {exc} ({wall:.1f}s)",
                    flush=True,
                )
                if attempt == 1:
                    await asyncio.sleep(30)  # 429/负载退避
                else:
                    audio = None
        if not audio:
            failures += 1
            manifest.append({"label": label, "article_id": aid, "chars": len(text), "error": True})
            continue
        path = out_dir / f"{label}.mp3"
        path.write_bytes(audio)
        cps = len(text) / max(wall, 0.1)
        manifest.append(
            {
                "label": label,
                "article_id": aid,
                "chars": len(text),
                "wall_s": round(wall, 1),
                "bytes": len(audio),
                "chars_per_wall_s": round(cps, 2),
            }
        )
        print(f"  ✓ {label}: {len(text)} 字  墙钟 {wall:.1f}s  {len(audio)} bytes", flush=True)

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "voice": voice,
                "model": getattr(client, "model", None),
                "base_url": getattr(client, "base_url", None),
                "chunk_chars": args.chunk_chars,
                "blocks": manifest,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n✅ 素材 → {out_dir}（失败 {failures} 次）", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="预设音色跨请求稳定性实验（采集端）")
    ap.add_argument("--out-dir", default="/tmp/tts_preset_stability")
    ap.add_argument("--chunk-chars", type=int, default=1500)
    ap.add_argument("--delay", type=float, default=15.0, help="请求间隔秒（上游负载敏感）")
    ap.add_argument("--scripts", type=int, default=4, help="取几篇生产真实稿")
    ap.add_argument("--text-file", help="自带文案（替代 DB 取稿）")
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
