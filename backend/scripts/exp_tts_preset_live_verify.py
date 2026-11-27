"""活体验证（2026-11-25）：部署后的声明式预设音色路由 + 全长渲染。

只读 DB 取一篇真实稿，走**部署代码**的预设分支：
    resolve_voice_for_user(None) → 婷婷(preset_voice=uncle_fu)
    use_preset = bool(preset_voice) and provider==openai
    client.synthesize(script, voice=preset_voice)   # 不带 ref_audio/ref_text
产物落 /tmp/tts_preset_live（**不写任何文章 / storage**）。
随后本机 ECAPA 判定：整段身份(vs 克隆源) + 滑窗自比(内部一致性)。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import text

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.tts_voice_service import resolve_voice_for_user
from stashbox.backend.app.services.tts import reload as tts_reload

OUT = Path("/tmp/tts_preset_live")
OUT.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    async with AsyncSessionLocal() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT article_id, script_text FROM distilled_articles "
                        "WHERE status='done' AND script_text IS NOT NULL "
                        "AND length(script_text) BETWEEN 2500 AND 3400 "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise SystemExit("no suitable script")
    aid, script = row["article_id"], row["script_text"]
    print(f"article={aid} chars={len(script)}", flush=True)

    resolved = await resolve_voice_for_user(None)
    print(
        f"resolved voice_id={resolved.voice_id} name={resolved.display_name} "
        f"source={resolved.source} preset_voice={resolved.preset_voice}",
        flush=True,
    )
    client = await tts_reload()
    print(
        f"provider={client.provider_name} chunk_chars={getattr(client,'chunk_chars',None)} "
        f"clone_chunk_chars={getattr(client,'clone_chunk_chars',None)}",
        flush=True,
    )
    use_preset = bool(resolved.preset_voice) and client.provider_name == "openai"
    print(f"use_preset={use_preset}", flush=True)
    assert use_preset, "preset routing FAILED"
    assert resolved.preset_voice == "uncle_fu", resolved.preset_voice

    t0 = time.time()
    audio = await client.synthesize(script, voice=resolved.preset_voice)
    dt = time.time() - t0
    fmt = "wav" if audio[:4] == b"RIFF" else "mp3"
    p = OUT / f"live_full.{fmt}"
    p.write_bytes(audio)
    meta = {
        "article_id": aid,
        "chars": len(script),
        "preset_voice": resolved.preset_voice,
        "voice_id": resolved.voice_id,
        "use_preset": use_preset,
        "provider": client.provider_name,
        "chunk_chars": getattr(client, "chunk_chars", None),
        "wall_s": round(dt, 1),
        "bytes": len(audio),
        "fmt": fmt,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"rendered bytes={len(audio)} wall={dt:.1f}s fmt={fmt} path={p}", flush=True)
    print("LIVE_OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
