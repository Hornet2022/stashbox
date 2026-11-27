"""对指定文章触发「重新生成」（distill/start），并轮询到终局。

用法（生产）：
  python scripts/exp_redistill_articles.py art_xxx art_yyy ...

语义：
  · `POST /api/v1/distill/start` = 仓内既有的「用当前音色重新生成」入口
    （ai-service main.py:406；保留旧产物，跑成功才覆盖 ⇒ 失败不丢旧音频）；
  · 幂等只挡 queued/running ⇒ 对已完成（done）的文章会真的重跑；
  · 属主鉴权 ⇒ 用文章属主的 token（uid 23 = 内测测试号）。
"""

import asyncio
import json
import sys
import time

sys.path.insert(0, "/opt/stashbox")

from sqlalchemy import text  # noqa: E402

from stashbox.backend.common.auth import create_access_token  # noqa: E402
from stashbox.backend.common.database import AsyncSessionLocal  # noqa: E402

AI_BASE = "http://127.0.0.1:8103"


async def trigger(article_id: str, token: str) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{AI_BASE}/api/v1/distill/start",
            headers={"Authorization": "Bearer " + token},
            json={"article_id": article_id},
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        return {"status": r.status_code, "body": body}


async def snapshot(article_id: str) -> dict:
    async with AsyncSessionLocal() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT da.status, length(da.script_text) AS slen, da.duration_sec, da.audio_url,"
                        " a.status AS astatus"
                        " FROM distilled_articles da JOIN articles a ON a.id=da.article_id"
                        " WHERE da.article_id=:a"
                    ),
                    {"a": article_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else {}


async def wait_done(article_id: str, timeout_s: int = 900) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        snap = await snapshot(article_id)
        key = (
            snap.get("status"),
            snap.get("astatus"),
            snap.get("duration_sec"),
            snap.get("audio_url"),
        )
        if key != last:
            print(
                f"    [{int(time.time()-t0):4d}s] status={snap.get('status')} articles={snap.get('astatus')} "
                f"dur={snap.get('duration_sec')} audio={(snap.get('audio_url') or '')[-24:]}",
                flush=True,
            )
            last = key
        if snap.get("status") in ("done", "failed"):
            return snap
        await asyncio.sleep(10)
    return await snapshot(article_id)


async def main() -> None:
    ids = sys.argv[1:]
    if not ids:
        print("用法：exp_redistill_articles.py <article_id...>")
        return
    token = create_access_token("23")
    out = []
    for aid in ids:
        print(f"\n=== 重新生成 {aid} ===", flush=True)
        before = await snapshot(aid)
        print(
            f"    重生成前: status={before.get('status')} dur={before.get('duration_sec')} "
            f"script={before.get('slen')}字",
            flush=True,
        )
        res = await trigger(aid, token)
        print(
            f"    触发: HTTP {res['status']} {json.dumps(res['body'], ensure_ascii=False)[:220]}",
            flush=True,
        )
        if res["status"] != 200:
            out.append({"article_id": aid, "trigger": res, "before": before})
            continue
        after = await wait_done(aid)
        print(
            f"    重生成后: status={after.get('status')} dur={after.get('duration_sec')} "
            f"script={after.get('slen')}字",
            flush=True,
        )
        out.append({"article_id": aid, "trigger": res, "before": before, "after": after})
    print("\nSUMMARY " + json.dumps(out, ensure_ascii=False))
    print("REDISTILL_DONE")


if __name__ == "__main__":
    asyncio.run(main())
