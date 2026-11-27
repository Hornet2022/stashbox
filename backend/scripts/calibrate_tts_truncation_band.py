"""重新标定 TTS 截断判据（2026-11-27）。

为什么重标：旧标定（2026-10-25）的「正常带 5.15~9.65」里**混进了被截断的样本**
  —— 同一个 250 字块两次实测 5.96 与 9.65，而 9.65 正是"音频只剩一半"的语速签名；
  于是阈值取 11.0 ⇒ 真截断（实测 8.14~9.44）**从阈值下面溜过去**。
  ⇒ 这是「坏统计量把防线抬高」的典型：判据的基线被污染，缺口反而无人守。

本脚本用**生产日志 + 生产稿件**重建样本：
  · 日志：每块都有「请求 UTF-8 字节」与「响应字节」；
  · 稿件：DB 的 script_text ⇒ 复现块边界 ⇒ 每块**真实字数**；
  · 时长：响应字节 / 上游码率（码率由「块合计时长 ≈ 实际时长」反推，已验证 ±3%）。
输出每块的 字数 / 时长 / cps / 篇内比值 ⇒ 定出新的阈值与自愈策略。

用法（生产）：python scripts/calibrate_tts_truncation_band.py
"""

import asyncio
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, "/opt/stashbox")

# 上游 mp3 码率（字节/秒）：由 4 篇实测「块合计时长 vs 实际时长」反推，±3%
UPSTREAM_BPS = 6800

RE_GROUP = re.compile(
    r"分段合成: total_chars=(\d+) chunks=(\d+) chunk_chars<=(\d+) concurrency=\d+ clone=(\w+)"
)
RE_REQ = re.compile(r"OpenAITTS synthesize: .* bytes=(\d+) .*clone=(\w+)")
RE_RESP = re.compile(r"OpenAITTS synthesized: (\d+) bytes")


def parse_journal() -> list[dict]:
    import subprocess

    raw = subprocess.run(
        "journalctl -u stashbox-ai-worker --since '2026-10-20' --no-pager 2>/dev/null | "
        "grep -E 'OpenAITTS synthesize:|OpenAITTS synthesized:|分段合成:'",
        shell=True,
        capture_output=True,
        text=True,
    ).stdout
    groups, cur = [], None
    for ln in raw.splitlines():
        m = RE_GROUP.search(ln)
        if m:
            cur = {
                "total_chars": int(m.group(1)),
                "n_chunks": int(m.group(2)),
                "chunk_chars": int(m.group(3)),
                "clone": m.group(4),
                "pairs": [],
            }
            groups.append(cur)
            continue
        m = RE_REQ.search(ln)
        if m and cur is not None:
            cur["pairs"].append({"req": int(m.group(1)), "resp": None})
            continue
        m = RE_RESP.search(ln)
        if m and cur is not None and cur["pairs"] and cur["pairs"][-1]["resp"] is None:
            cur["pairs"][-1]["resp"] = int(m.group(1))
    return [
        g for g in groups if len(g["pairs"]) == g["n_chunks"] and all(p["resp"] for p in g["pairs"])
    ]


async def main() -> None:
    from sqlalchemy import text

    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks
    from stashbox.backend.common.database import AsyncSessionLocal

    groups = parse_journal()
    print(f"日志分组（配齐）{len(groups)} 个\n")

    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT da.article_id, da.script_text FROM distilled_articles da "
                        "WHERE da.script_text IS NOT NULL"
                    )
                )
            )
            .mappings()
            .all()
        )
    by_len: dict[int, list] = {}
    for r in rows:
        by_len.setdefault(len(r["script_text"]), []).append(r)

    out = []
    for g in groups:
        if g["clone"] != "off":
            continue  # 只标定当前在用的预设路径
        cands = by_len.get(g["total_chars"], [])
        if not cands:
            print(f"  跳过 total_chars={g['total_chars']}（DB 里找不到同样字数的稿件）")
            continue
        cand = cands[0]
        chunks = split_into_chunks(cand["script_text"], g["chunk_chars"])
        if len(chunks) != g["n_chunks"]:
            print(
                f"  跳过 {cand['article_id']}：块数对不上（本地 {len(chunks)} vs 日志 {g['n_chunks']}）"
            )
            continue
        cpss = []
        for c, p in zip(chunks, g["pairs"]):
            dur = p["resp"] / UPSTREAM_BPS
            cpss.append(
                {
                    "chars": len(c),
                    "bytes": len(c.encode("utf-8")),
                    "req": p["req"],
                    "resp": p["resp"],
                    "dur": round(dur, 1),
                    "cps": round(len(c) / dur, 2),
                }
            )
        med = statistics.median([c["cps"] for c in cpss])
        print(
            f"=== {cand['article_id']}  稿 {g['total_chars']} 字 / {g['n_chunks']} 块 "
            f"(chunk_chars={g['chunk_chars']}) ==="
        )
        for i, c in enumerate(cpss):
            flag = ""
            if c["cps"] / med > 1.3:
                flag = "  ← 语速离群（疑似被截断）"
            print(
                f"  chunk{i}: {c['chars']:4d}字 {c['bytes']:5d}B resp={c['resp']:8d} "
                f"⇒ {c['dur']:6.1f}s ⇒ {c['cps']:5.2f} 字/秒  比值={c['cps']/med:5.2f}{flag}"
            )
        out.append(
            {
                "article": cand["article_id"],
                "total_chars": g["total_chars"],
                "chunk_chars": g["chunk_chars"],
                "median_cps": round(med, 2),
                "chunks": cpss,
            }
        )

    allcps = [c["cps"] for o in out for c in o["chunks"]]
    allratio = [c["cps"] / o["median_cps"] for o in out for c in o["chunks"]]
    if allcps:
        srt = sorted(allcps)
        n = len(srt)
        print(f"\n预设路径全样本 {n} 块（字数为真实稿件切分）:")
        for p in (0, 5, 25, 50, 75, 95, 100):
            print(f"  cps p{p:<3d} = {srt[min(n-1, int(n*p/100))]:.2f}")
        srt_r = sorted(allratio)
        print("\n篇内比值全样本:")
        for p in (0, 5, 25, 50, 75, 95, 100):
            print(f"  ratio p{p:<3d} = {srt_r[min(n-1, int(n*p/100))]:.2f}")
    Path("/tmp/chunk_replay/calibration.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nWROTE /tmp/chunk_replay/calibration.json")


if __name__ == "__main__":
    asyncio.run(main())
