#!/usr/bin/env python3
"""redistill_voices.py — 批量重跑「音色不一致」的存量音频。

## 为什么需要这个（2026-10-24）

「分段音色差异」的根因是上游**每次渲染随机挑一个说话人**：
同一段文本、同一个 voice 名连发 3 次 → 基频中位数 165.0 / 188.5 / 105.3 Hz（3 个人）。
我们按 700 字分块 → 每块一次请求 → **每块一个人**。

修法是让零样本克隆真的生效（已上线）。但**存量音频是已经落盘的文件**，
代码改了不会回头改它们 —— 存量那 29 篇还是「好几个人在念」。

⚠️ 本脚本**默认只读**（dry-run）：列清单 + 估算耗时，不动任何东西。
    真跑要加 `--apply`。

## 谁该被重跑

    distilled_articles 里 status='done' 且有 audio_url，
    但 **tts_voice_id 为空** —— 即「不是用音色库里的音色合成的」。

    tts_voice_id 非空的（已经用克隆重跑过）→ 跳过。

## 用法

    # 只看清单与估算（默认，不动数据）
    python scripts/redistill_voices.py

    # 真跑（逐篇串行，每篇之间有间隔，避免压垮上游）
    python scripts/redistill_voices.py --apply

    # 只重跑某几篇
    python scripts/redistill_voices.py --apply --article art_xxx --article art_yyy

    # 限速（默认 20s 一篇）
    python scripts/redistill_voices.py --apply --delay 30

## 为什么走 HTTP 端点而不是直接入队

    `POST /api/v1/articles/{id}/distill` 里已经有配额幂等、入队幂等、状态机、埋点 ——
    那些都是生产同一条路。直接入队会绕过它们，验的就不是生产路径了。

    端点要 `require_user` 且校验归属，所以脚本用**服务端自己的 JWT_SECRET**
    签一个该文章属主的短期 token（60 分钟过期）—— 密钥不出服务器。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── 包路径自举 ─────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

AI_SERVICE = os.getenv("STASHBOX_AI_SERVICE_URL", "http://127.0.0.1:8103")


def _rows(sql: str) -> list[list[str]]:
    """查库。

    ⚠️ 用 `--csv` 而不是 `-F '\\t'`：文章标题里可能有换行/制表符
    → 按分隔符切会把一行拆成两行（实测报 IndexError）。CSV 有正经的引号转义。
    """
    import csv
    import io
    import subprocess

    pg = os.getenv("POSTGRES_PASSWORD", "")
    env = dict(os.environ, PGPASSWORD=pg)
    out = subprocess.run(
        [
            "psql",
            "-h",
            os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "-U",
            os.getenv("POSTGRES_USER", "stashbox"),
            "-d",
            os.getenv("POSTGRES_DB", "stashbox"),
            "--csv",
            # ⚠️ psql 的 --csv 默认分隔符是 **|**（历史默认），不是逗号
            "--field-separator=,",
            "-A",
            "-t",
            "-c",
            sql,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return [row for row in csv.reader(io.StringIO(out.stdout)) if row]


def candidates(only: list[str] | None) -> list[dict]:
    """要重跑的存量文章（tts_voice_id 为空 = 不是用音色库音色合成的）。"""
    sql = (
        "select d.article_id, a.user_id, coalesce(d.duration_sec,0), "
        "length(coalesce(d.script_text,'')), "
        # ⚠️ 标题里可能有换行 → psql 的 --csv **不会**给换行加引号
        # → 那一行会被拆成两行。查询里先把换行换成空格。
        "replace(replace(coalesce(a.title,''), chr(10), ' '), chr(13), ' '), "
        "d.audio_url, a.created_at::text "
        "from distilled_articles d join articles a on a.id = d.article_id "
        "where d.status='done' and d.audio_url is not null and d.tts_voice_id is null "
        "order by a.created_at"
    )
    out = []
    for r in _rows(sql):
        item = {
            "article_id": r[0],
            "user_id": r[1],
            "duration_sec": int(r[2] or 0),
            "script_len": int(r[3] or 0),
            "title": r[4][:30],
            "audio_url": r[5],
            "created_at": r[6],
        }
        if only and item["article_id"] not in only:
            continue
        out.append(item)
    return out


def _token(user_id: str) -> str:
    from stashbox.backend.common.auth import create_access_token

    return create_access_token(user_id, extra={"tier": "free"}, expire_minutes=60)


def _http(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description="批量重跑音色不一致的存量音频")
    ap.add_argument("--apply", action="store_true", help="真跑（默认只列清单）")
    ap.add_argument("--article", action="append", help="只重跑指定文章（可多次）")
    ap.add_argument("--delay", type=int, default=20, help="每篇之间的间隔秒数（默认 20）")
    ap.add_argument("--timeout", type=int, default=900, help="单篇等待完成的秒数（默认 900）")
    args = ap.parse_args()

    items = candidates(args.article)
    total_min = sum(i["duration_sec"] for i in items) / 60
    print(f"待重跑 {len(items)} 篇，合计 {total_min:.1f} 分钟旧音频")
    print(
        f"预计耗时：每篇约 2~4 分钟（LLM 改写 + {max(1, -(-items[0]['script_len'] // 250)) if items else 0} "
        f"块克隆渲染）→ 串行约 {len(items) * 3} 分钟"
        if items
        else "待重跑 0 篇"
    )
    for i in items:
        print(
            f"  {i['article_id']}  user={i['user_id']:>3}  稿 {i['script_len']:>4} 字  "
            f"旧音频 {i['duration_sec']:>4}s  {i['created_at'][:10]}  {i['title']}"
        )

    if not args.apply:
        print("\n（dry-run：加 --apply 才真跑）")
        return 0
    if not items:
        return 0

    print("\n开始重跑：")
    for n, item in enumerate(items, 1):
        token = _token(item["user_id"])
        status, resp = _http(
            "POST", f"{AI_SERVICE}/api/v1/articles/{item['article_id']}/distill", token
        )
        print(f"[{n}/{len(items)}] {item['article_id']} 触发 HTTP {status} {str(resp)[:120]}")
        if status != 200:
            continue
        task_id = resp.get("task_id") or ""
        deadline = time.time() + args.timeout
        last = ""
        while time.time() < deadline:
            time.sleep(15)
            st, body = _http("GET", f"{AI_SERVICE}/api/v1/distill/{task_id}", token)
            cur = body.get("status") if isinstance(body, dict) else str(body)
            if cur != last:
                print(f"      状态 → {cur}")
                last = cur
            if cur in ("done", "failed", "skipped"):
                break
        else:
            print("      ⚠️ 超时未完成")
        if n < len(items):
            time.sleep(args.delay)
    print("\n完成。可用 backend/scripts/check_tts_voice_consistency.py 复验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
