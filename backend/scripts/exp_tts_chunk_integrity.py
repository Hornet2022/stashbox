"""上游单请求长度上限标定（2026-11-26）。

背景：生产 OPENAI_TTS_CHUNK_CHARS 被调到 1500 后，新蒸馏音频**静默丢内容**
（ASR 对照：稿里 related[2]+related[3]+insights[0] 共 ~397 字在音频里整块消失）。
机制假设：上游单请求有长度上限，超出部分被丢弃（不是报错）。

本脚本对同一篇稿（生产真稿 script_text）按不同块长**逐块**合成并落盘，
用来做两件事：
  1. 逐块记录「字数 / 音频时长 / cps」⇒ 看截断随块长的变化；
  2. 每块单独落盘 ⇒ 本机可用 ASR 抽查块尾内容（判"块是否念完"）。

用法（生产）：
  OPENAI_TTS_CHUNK_CHARS=700 ... python scripts/exp_tts_chunk_integrity.py
产出：/tmp/chunk_probe/{cs}_{i}.mp3  + /tmp/chunk_probe/manifest.json
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/stashbox")
OUT = Path("/tmp/chunk_probe")
OUT.mkdir(parents=True, exist_ok=True)

ARTICLE_ID = "art_dc2c2e3283f94663857a73f6"
CHUNK_SIZES = [int(x) for x in os.getenv("PROBE_CHUNK_SIZES", "700,1000,1500").split(",")]
VOICE = os.getenv("PROBE_VOICE", "uncle_fu")
DELAY = float(os.getenv("PROBE_DELAY", "3"))


async def main() -> None:
    from sqlalchemy import text
    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks

    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT script_text FROM distilled_articles WHERE article_id=:a"),
                {"a": ARTICLE_ID},
            )
        ).first()
    script = row[0] if row else ""
    if not script:
        print("NO_SCRIPT")
        return
    print(f"稿件字符数(len)={len(script)}")

    client = await tts_reload()
    print(
        f"provider={client.provider_name} chunk_chars(env)={getattr(client, 'chunk_chars', None)}"
    )

    manifest = []
    for cs in CHUNK_SIZES:
        chunks = split_into_chunks(script, cs)
        print(f"\n=== chunk_chars={cs} → {len(chunks)} 块 ===")
        for i, ch in enumerate(chunks):
            t0 = time.time()
            try:
                data = await client._synthesize_chunk(ch, VOICE, "mp3")
            except Exception as exc:  # noqa: BLE001
                print(f"  块{i} 合成失败: {exc}")
                manifest.append({"cs": cs, "idx": i, "chars": len(ch), "error": str(exc)})
                await asyncio.sleep(DELAY)
                continue
            wall = time.time() - t0
            p = OUT / f"{cs}_{i}.mp3"
            p.write_bytes(data)
            head = ch[:14].replace("\n", " ")
            tail = ch[-14:].replace("\n", " ")
            print(f"  块{i}: chars={len(ch)} bytes={len(data)} wall={wall:.1f}s -> {p.name}")
            print(f"       头: {head}")
            print(f"       尾: {tail}")
            manifest.append(
                {
                    "cs": cs,
                    "idx": i,
                    "chars": len(ch),
                    "bytes": len(data),
                    "wall": round(wall, 1),
                    "file": str(p),
                    "head": head,
                    "tail": tail,
                }
            )
            await asyncio.sleep(DELAY)

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nmanifest -> {OUT/'manifest.json'}")
    print("CHUNK_PROBE_DONE")


if __name__ == "__main__":
    asyncio.run(main())
