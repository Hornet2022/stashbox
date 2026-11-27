"""单块重放实验：截断是**确定性**（内容相关）还是**偶发**（上游/传输抖动）。

背景（2026-11-27 生产实证）：
  用生产日志的「请求字节 / 响应字节」逐块算语速（不依赖 ASR），
  发现 4 篇重生成音频里有 2 篇各有一块只产出约一半音频：
      art_a7ebe143 chunk1  690字/1760B → 9.44 字/秒（同篇其他块 4.3~4.7）
      art_d00811fb chunk2  679字/1939B → 8.14 字/秒（同篇其他块 4.4~5.1）
  ⇒ 块长 700 并不足以保证内容完整；且现有 `_TRUNCATION_CHARS_PER_SEC=11.0`
    **抓不到**（8.14~9.44 < 11.0）。

本脚本对指定块**逐次单独重放**（同一段文本、同一音色、连续 N 次），
记录每次的音频时长与语速 ⇒ 区分：
  · 每次都短  ⇒ **确定性**：上游对这段文本就是只念一半 ⇒ 只能靠「切小块/换策略」解决；
  · 时长忽长忽短 ⇒ **偶发**：重渲即可救 ⇒ 运行时「检出即重渲」就能闭环。

另外对同一块做「二分重渲」对照（把块切两半分别合成再拼），
看总时长是否补齐 ⇒ 验证仓内既有的二分自愈是否真能救回这类截断。

用法（生产）：
  PROBE_ARTICLES="art_xxx:1,art_yyy:2" PROBE_REPEAT=3 \
    python scripts/exp_tts_chunk_replay.py
产出：/tmp/chunk_replay/<article>_c<i>_<n>.mp3 + /tmp/chunk_replay/manifest.json
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/stashbox")

OUT = Path("/tmp/chunk_replay")
OUT.mkdir(parents=True, exist_ok=True)

VOICE = os.getenv("PROBE_VOICE", "uncle_fu")
REPEAT = int(os.getenv("PROBE_REPEAT", "3"))
DELAY = float(os.getenv("PROBE_DELAY", "3"))
# 文章 = 待重放块下标（逗号分隔的 "article_id:chunk_index"）
TARGETS = os.getenv("PROBE_ARTICLES", "")


async def main() -> None:
    from sqlalchemy import text

    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks
    from stashbox.backend.app.services.tts.audio_duration import audio_duration_exact_sec

    client = await tts_reload()
    print(
        f"provider={client.provider_name} chunk_chars={getattr(client, 'chunk_chars', None)} "
        f"clone={getattr(client, 'clone_chunk_chars', None)}"
    )

    manifest = []
    for spec in [s for s in TARGETS.split(",") if s.strip()]:
        aid, _, idx_s = spec.partition(":")
        idx = int(idx_s or "0")
        from stashbox.backend.common.database import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    text("SELECT script_text FROM distilled_articles WHERE article_id=:a"),
                    {"a": aid},
                )
            ).first()
        script = row[0] if row else ""
        if not script:
            print(f"NO_SCRIPT {aid}")
            continue
        chunks = split_into_chunks(script, 700)
        if idx >= len(chunks):
            print(f"NO_CHUNK {aid} idx={idx} (共 {len(chunks)} 块)")
            continue
        ch = chunks[idx]
        print(f"\n===== {aid} chunk{idx}  {len(ch)} 字 / {len(ch.encode('utf-8'))} B =====")
        print(f"块首: {ch[:40]}")
        print(f"块尾: {ch[-40:]}")

        for n in range(1, REPEAT + 1):
            t0 = time.time()
            try:
                # ⚠️ 用 _synthesize_once（不经 _assert_not_truncated / 二分自愈）——
                #    要看的是**上游原始返回**，不是自愈后的结果。
                data = await client._synthesize_once(ch, VOICE, "mp3")
            except Exception as exc:  # noqa: BLE001
                print(f"  第{n}次 合成失败: {exc}")
                manifest.append({"article": aid, "chunk": idx, "n": n, "error": str(exc)})
                await asyncio.sleep(DELAY)
                continue
            dur = audio_duration_exact_sec(data)
            cps = len(ch) / dur if dur else 0
            path = OUT / f"{aid}_c{idx}_{n}.mp3"
            path.write_bytes(data)
            print(
                f"  第{n}次: bytes={len(data)} 时长={dur:.1f}s ⇒ {cps:.2f} 字/秒 "
                f"墙钟={time.time()-t0:.1f}s → {path.name}"
            )
            manifest.append(
                {
                    "article": aid,
                    "chunk": idx,
                    "n": n,
                    "chars": len(ch),
                    "bytes": len(data),
                    "dur": round(dur, 2) if dur else None,
                    "cps": round(cps, 2),
                    "wall": round(time.time() - t0, 1),
                    "file": path.name,
                }
            )
            await asyncio.sleep(DELAY)

        # ── 二分对照：把同一块切两半分别合成，看总时长能否补齐 ──
        parts = split_into_chunks(ch, max(1, len(ch) // 2))
        if len(parts) >= 2:
            print(f"  ── 二分对照：切成 {len(parts)} 块 ──")
            total = 0.0
            for pi, p in enumerate(parts):
                try:
                    data = await client._synthesize_once(p, VOICE, "mp3")
                except Exception as exc:  # noqa: BLE001
                    print(f"    半块{pi} 失败: {exc}")
                    await asyncio.sleep(DELAY)
                    continue
                d = audio_duration_exact_sec(data)
                total += d or 0
                pp = OUT / f"{aid}_c{idx}_half{pi}.mp3"
                pp.write_bytes(data)
                print(f"    半块{pi}: {len(p)}字 ⇒ {d:.1f}s ({len(p)/d:.2f} 字/秒) → {pp.name}")
                await asyncio.sleep(DELAY)
            print(f"    二分合计 {total:.1f}s vs 单次 {manifest[-1]['dur']}s")
            manifest.append(
                {
                    "article": aid,
                    "chunk": idx,
                    "halves_total_dur": round(total, 2),
                    "halves": len(parts),
                }
            )

    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWROTE {OUT/'manifest.json'}")


if __name__ == "__main__":
    asyncio.run(main())
