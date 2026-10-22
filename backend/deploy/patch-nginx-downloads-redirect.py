#!/usr/bin/env python3
"""把**域名入口**的 /downloads/ 改成 302 到 IP 入口（备案通过前）。

## 背景

2026-10-22 实测：未备案的域名在 443 上**小请求正常、大流量被掐**。
同一个 IP、同一个网络、同一个文件：

    GET /downloads/…apk  200  3836512  AndroidDownloadManager/16  ← 完整
    GET /downloads/…apk  200   147456  MiuiBrowser                 ← 断

服务器侧全清白（无 OOM、nginx 没重启、Range 206 正常、出网 578MB 远未到
配额）→ 是「未备案域名的流量」被区别对待。

所以备案通过前，**大流量分发一律走 IP**：
  · 域名入口的 /downloads/ → 302 到 https://8.148.9.151$request_uri
  · IP 入口（server_name _）的 /downloads/ → 保持直出（不能再跳自己）

App 内的下载（应用内更新）走 App 自己的兜底，不经过这里。

## 为什么用 patch 而不是直接改配置文件

服务器上的站点配置已经被今天的多个 patch 脚本改过，与仓库模板**已经分叉**
（仓库模板里没有公开面块，那块是 patch-nginx-site.py 生成的）。
直接整文件覆盖会把那些改动抹掉。所以这里**按 server 块定位**、只换 include
那一行。

## 幂等

已经改过的块会跳过。备案通过后要改回来：把三个域名块的 include 换回
`stashbox-downloads.conf`（直出），并删掉 redirect 片段。

用法：
    python3 patch-nginx-downloads-redirect.py [--check]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path("/etc/nginx/sites-available/stashbox")
SNIPPET_DIR = Path("/etc/nginx/snippets")
REDIRECT_SRC = Path(__file__).resolve().parent / "systemd/snippets/stashbox-downloads-redirect.conf"
BACKUP_DIR = Path("/etc/nginx/backup")

DIRECT = "    include /etc/nginx/snippets/stashbox-downloads.conf;"
REDIRECT = "    include /etc/nginx/snippets/stashbox-downloads-redirect.conf;"


def server_blocks(text: str) -> list[tuple[int, int, str]]:
    """返回 (start, end, block_text)。"""
    out = []
    for m in re.finditer(r"^server\s*\{(?:(?!^\}).)*?^\}", text, re.S | re.M):
        out.append((m.start(), m.end(), m.group(0)))
    return out


def patch_site(text: str) -> tuple[str, list[str]]:
    changed_notes: list[str] = []
    blocks = server_blocks(text)
    # 从后往前改，避免偏移量失效
    for start, end, block in reversed(blocks):
        m = re.search(r"server_name\s+([^;]+);", block)
        if not m:
            continue
        names = m.group(1).split()
        # IP 兜底块（server_name _）必须保持直出 —— 否则会 302 到自己
        if names == ["_"]:
            continue
        if DIRECT not in block:
            continue
        new_block = block.replace(DIRECT, REDIRECT)
        text = text[:start] + new_block + text[end:]
        changed_notes.append(" ".join(names))
    return text, changed_notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not SITE.exists():
        print(f"✗ 找不到 {SITE}", file=sys.stderr)
        return 2

    before = SITE.read_text()
    after, notes = patch_site(before)

    print(f"域名入口改 302 的块：{notes or '（无 —— 已就位或未匹配）'}")
    print("IP 入口（server_name _）保持直出")

    if args.check:
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = subprocess.run(
        ["date", "+%Y%m%d-%H%M%S"], capture_output=True, text=True, check=False
    ).stdout.strip()
    shutil.copy2(SITE, BACKUP_DIR / f"stashbox.{stamp}")

    SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REDIRECT_SRC, SNIPPET_DIR / "stashbox-downloads-redirect.conf")

    SITE.write_text(after)

    if subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=False).returncode != 0:
        shutil.copy2(BACKUP_DIR / f"stashbox.{stamp}", SITE)
        print("✗ nginx -t 失败，已回滚", file=sys.stderr)
        return 1

    subprocess.run(["systemctl", "reload", "nginx"], check=False)
    print("✓ 已应用并 reload nginx（备份在 /etc/nginx/backup/）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
