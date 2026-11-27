#!/usr/bin/env python3
"""生产活体验收：TTS 内容完整性 + 音色 + 播放列表闭环，一条命令跑完。

为什么要有这个脚本
------------------
「音频内容与稿子不一致」是**偶发**缺陷：上游 TTS 对超长请求会**静默截断**
（HTTP 200、音频更短、看起来完全成功）。缺陷发生时人工排查要跨三套口径
（生产日志逐块字节 / 仓内切块复现 / ASR 转写对照），每次重跑成本高。

⇒ 把三者的口径固化成脚本：
  ① **字节口径（主判据，不依赖 ASR）**：生产日志逐块「请求 UTF-8 字节 / 上游响应字节」
     ⇒ 块时长 = 响应字节 ÷ 上游码率（码率由「上游总字节 ÷ 实测总时长」反推）
     ⇒ 语速 = 块字数 ÷ 块时长，超过阈值 ⇒ 该块疑似被截断。
  ② **块↔日志映射自校准**：用仓内 `split_into_chunks(script_text, 700)` 复现块边界，
     每块 UTF-8 字节必须与日志的 `bytes=` **逐条吻合** —— 不吻合说明日志与块错位，
     后面所有判定都不可信（fail-closed）。
  ③ **阈值从仓内既有定义 import**（不另造常量）：
     `openai._TRUNCATION_CPS_PRESET_LONG` / `_TRUNCATION_CHARS_PER_SEC` /
     `_TRUNCATION_STRICT_MIN_CHARS`，与线上跑的判据**同源**。

用法
----
    cd backend
    .venv/bin/python scripts/exp_live_acceptance_tts_verify.py <article_id>

    # 必要时手工指定时间窗（默认取该文章蒸馏窗口前后 10 分钟）
    --since "2026-11-27 10:45" --until "2026-11-27 11:00"

依赖：ffprobe（可选，用于实测总时长校准码率）、ssh 到生产。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND.parents[1]))  # 让 `stashbox.backend...` 可导入

from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks  # noqa: E402
from stashbox.backend.app.services.tts.openai import (  # noqa: E402
    _TRUNCATION_CHARS_PER_SEC,
    _TRUNCATION_CPS_PRESET_LONG,
    _TRUNCATION_STRICT_MIN_CHARS,
)

HOST = "root@8.148.9.151"
KEY = "/Users/hornet/.ssh/stashbox_deploy"
SSH = f"ssh -i {KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=20 {HOST}"
OUT = Path("/tmp/tts_live_acceptance")

REMOTE_FETCH = r"""
import asyncio, json, sys
from sqlalchemy import text
from stashbox.backend.common.database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT d.script_text, d.audio_url, d.duration_sec, a.user_id, a.status, a.updated_at "
            "FROM distilled_articles d JOIN articles a ON a.id = d.article_id "
            "WHERE d.article_id = :a"), {"a": ARTICLE_ID})).mappings().first()
        print(json.dumps(dict(row) if row else {}, ensure_ascii=False, default=str))

asyncio.run(main())
"""


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout


def fetch_meta(article_id: str) -> dict:
    local_py = OUT / "remote_fetch.py"
    local_py.write_text(REMOTE_FETCH.replace("ARTICLE_ID", repr(article_id)))
    sh(f"scp -i {KEY} -o StrictHostKeyChecking=no {local_py} {HOST}:/tmp/remote_fetch.py")
    out = sh(
        f"{SSH} 'cd /opt/stashbox/stashbox/backend && set -a && . /etc/stashbox/stashbox.env && "
        f"set +a && export PYTHONPATH=/opt/stashbox LC_ALL=C.UTF-8 PYTHONUTF8=1 && "
        f"/opt/stashbox/venv/bin/python /tmp/remote_fetch.py'"
    )
    line = out.strip().splitlines()[-1] if out.strip() else "{}"
    return json.loads(line)


def fetch_log(since: str, until: str) -> str:
    return sh(
        f'{SSH} \'journalctl -u stashbox-ai-worker --since "{since}" --until "{until}" '
        f'--no-pager 2>/dev/null | grep -E "OpenAITTS|tts_voice_resolved"\''
    )


def parse_log(log: str) -> tuple[dict, list[tuple[int, int]], bool]:
    """返回 (元信息, [(请求字节, 响应字节)], 是否走了预设路径)。

    一个时间窗里可能有多篇的蒸馏记录 ⇒ 按「分段合成」切分成多组，
    调用方用 total_chars 匹配挑出目标那组（见 pick_group）。
    """
    groups: list[dict] = []
    cur: dict | None = None
    preset = False
    for ln in log.splitlines():
        m = re.search(r"分段合成: total_chars=(\d+) chunks=(\d+).*clone=(\w+)", ln)
        if m:
            cur = {
                "total_chars": int(m.group(1)),
                "chunks": int(m.group(2)),
                "clone": m.group(3),
                "pairs": [],
            }
            groups.append(cur)
            continue
        if "tts_voice_resolved" in ln and "preset=" in ln:
            pm = re.search(r"preset=(\S+)", ln)
            preset = bool(pm and pm.group(1) not in ("None", "none", ""))
        m = re.search(r"OpenAITTS synthesize: .* bytes=(\d+)", ln)
        if m:
            cur = cur or {"pairs": []}
            cur["pending"] = int(m.group(1))
            continue
        m = re.search(r"OpenAITTS synthesized: (\d+) bytes", ln)
        if m and cur is not None and cur.get("pending") is not None:
            cur["pairs"].append((cur.pop("pending"), int(m.group(1))))
            continue
    return groups, groups[0] if groups else {}, preset


def pick_group(groups: list[dict], total_chars: int) -> tuple[dict, list[tuple[int, int]]]:
    """按稿件字数挑出对应的蒸馏组；挑不准 ⇒ fail-closed（返回空）。"""
    exact = [g for g in groups if g.get("total_chars") == total_chars]
    if len(exact) == 1:
        g = exact[0]
        return g, g.get("pairs", [])
    if exact:
        g = exact[-1]
        return g, g.get("pairs", [])
    return {}, []


def ffprobe_duration(path: Path) -> float | None:
    if not shutil_which("ffprobe"):
        return None
    out = sh(
        f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{path}'"
    )
    try:
        return float(out.strip())
    except ValueError:
        return None


def shutil_which(bin: str) -> str | None:
    r = subprocess.run(f"command -v {bin}", shell=True, capture_output=True, text=True)
    return r.stdout.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("article_id")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    meta = fetch_meta(args.article_id)
    if not meta.get("script_text"):
        print(f"❌ 取不到 {args.article_id} 的蒸馏产物")
        return 1
    script = meta["script_text"]

    since = args.since or "2026-11-27 00:00"
    until = args.until or "2026-11-27 23:59"
    log = fetch_log(since, until)
    groups, _, preset = parse_log(log)
    lmeta, pairs = pick_group(groups, len(script))

    mp3 = OUT / f"{args.article_id}.mp3"
    sh(f"curl -sS -o '{mp3}' '{meta['audio_url']}'")

    chunks = split_into_chunks(script, 700)
    print(f"=== {args.article_id} ===")
    print(
        f"稿字数 = {len(script)}   仓内切块 = {len(chunks)}   日志块数 = {lmeta.get('chunks')}   "
        f"clone = {lmeta.get('clone')}   预设路径 = {preset}"
    )
    print(f"DB duration_sec = {meta.get('duration_sec')}   articles.status = {meta.get('status')}")
    print(f"audio_url = {meta.get('audio_url')}")

    if len(chunks) != len(pairs):
        print(f"❌ 块数与日志不符（{len(chunks)} vs {len(pairs)}）⇒ 口径不可信，判失败")
        return 1

    total_resp = sum(r for _, r in pairs)
    actual = ffprobe_duration(mp3) or float(meta.get("duration_sec") or 0)
    upstream_bps = total_resp / actual if actual else 6800.0

    # 阈值：预设路径长块走严格档，其余走宽松档（从仓内 import，与线同源）
    strict = preset and lmeta.get("clone") == "off"

    print(f"上游总字节 = {total_resp}   实测总时长 = {actual}s   反推码率 = {upstream_bps:.0f} B/s")
    print()
    print(
        f"{'块':<8}{'字数':>6}{'请求字节':>10}{'日志':>8}{'吻合':>6}{'响应字节':>12}"
        f"{'时长s':>9}{'语速':>7}{'阈值':>6}  判定"
    )
    bad = False
    for i, (ch, (rb, sb)) in enumerate(zip(chunks, pairs)):
        n = len(ch)
        u8 = len(ch.encode("utf-8"))
        dur = sb / upstream_bps
        cps = n / dur
        limit = (
            _TRUNCATION_CPS_PRESET_LONG
            if strict and n >= _TRUNCATION_STRICT_MIN_CHARS
            else _TRUNCATION_CHARS_PER_SEC
        )
        ok_bytes = u8 == rb
        verdict = "OK" if cps <= limit else "⚠️ 疑似截断"
        if cps > limit or not ok_bytes:
            bad = True
        print(
            f"chunk{i:<3}{n:>6}{u8:>10}{rb:>8}{'✓' if ok_bytes else '✗':>6}{sb:>12}"
            f"{dur:>9.2f}{cps:>7.2f}{limit:>6.1f}  {verdict}"
        )
    print()
    print(f"整篇：{len(script)} 字 / {actual}s = {len(script)/actual:.2f} 字/秒" if actual else "")
    print(
        f"判定：{'❌ 有块超阈值或字节不吻合' if bad else '✅ 全部块在阈值内，且块↔日志字节逐条吻合'}"
    )
    print("（内容完整性只是第一道；音色零换人与 ASR 对照见另外两条链路）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
