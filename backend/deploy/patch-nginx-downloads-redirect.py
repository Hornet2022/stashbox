#!/usr/bin/env python3
"""切换**域名入口**的 /downloads/ 是「直出」还是「302 到 IP 入口」。

## 两个态

| 态 | 域名入口的 /downloads/ | 什么时候 |
|---|---|---|
| **302** | `return 302 https://8.148.9.151$request_uri` | ICP 备案**未过**（云厂商对未备案域名的流量做限制，大流量被掐） |
| **直出** | `alias /var/www/stashbox-downloads/`（自己吐文件） | ICP 备案**已过**（当前态，2026-10-23） |

IP 入口（`server_name _`）**始终直出** —— 不能跳自己。

## 为什么要做成双向

302 那一态是**为备案未过准备的临时措施**，备案通过后必须改回直出：
未备案时域名的大流量会被掐，302 到 IP 是绕开这个限制；
备案后限制解除，302 就成了**多一跳**（多一次跨主机跳转，
对下载器不友好），而且营销页的链接语义变成了「域名只是个跳转器」。

但反向也可能需要 —— 云厂商的策略会变、域名线路也可能出问题。
所以做成双向开关，任何时候一条命令能切回去，而不是临时手改 nginx。

## 背景（2026-10-22 实测，302 那一态的由来）

未备案的域名在 443 上**小请求正常、大流量被掐**。同一个 IP、同一个网络、
同一个文件：

    GET /downloads/…apk  200  3836512  AndroidDownloadManager/16  ← 完整
    GET /downloads/…apk  200   147456  MiuiBrowser                 ← 断

服务器侧全清白（无 OOM、nginx 没重启、Range 206 正常、出网 578MB 远未到
配额）→ 是「未备案域名的流量」被区别对待。

⚠️ 注意后来的复盘（2026-10-23）：那条断流**就发生在 IP 入口上**
（Chrome 经 302 之后的 IP 请求只传了 224KB），所以
「IP 直连能绕过」这个假设当时就不成立 —— 302 那一态真正解决的是
「未备案域名被限流」，不是「大流量必然断」。

## 为什么用 patch 而不是直接改配置文件

服务器上的站点配置已经被多个 patch 脚本改过，与仓库模板**已经分叉**
（仓库模板里没有公开面块，那块是 patch-nginx-site.py 生成的）。
直接整文件覆盖会把那些改动抹掉。所以这里**按 server 块定位**、只换 include
那一行。

## 幂等

已经处于目标态的块会跳过。

用法：
    python3 patch-nginx-downloads-redirect.py            # 切到 302（备案未过）
    python3 patch-nginx-downloads-redirect.py --direct   # 切回直出（备案已过）
    python3 patch-nginx-downloads-redirect.py --check     # 只看当前状态
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


def patch_site(text: str, to_direct: bool) -> tuple[str, list[str]]:
    """把**域名块**的 downloads include 切到目标态。

    IP 兜底块（server_name _）**必须保持直出** —— 否则会 302 到自己。
    """
    changed_notes: list[str] = []
    blocks = server_blocks(text)
    src, dst = (REDIRECT, DIRECT) if to_direct else (DIRECT, REDIRECT)
    # 从后往前改，避免偏移量失效
    for start, end, block in reversed(blocks):
        m = re.search(r"server_name\s+([^;]+);", block)
        if not m:
            continue
        names = m.group(1).split()
        if names == ["_"]:
            continue
        if src not in block:
            continue
        text = text[:start] + block.replace(src, dst) + text[end:]
        changed_notes.append(" ".join(names))
    return text, changed_notes


def current_state(text: str) -> list[str]:
    """当前哪些域名块用的是 302 / 直出。"""
    redirect_blocks, direct_blocks = [], []
    for _, _, block in server_blocks(text):
        m = re.search(r"server_name\s+([^;]+);", block)
        if not m:
            continue
        names = m.group(1).split()
        if names == ["_"]:
            continue
        if REDIRECT in block:
            redirect_blocks.append(" ".join(names))
        elif DIRECT in block:
            direct_blocks.append(" ".join(names))
    return redirect_blocks, direct_blocks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--direct",
        action="store_true",
        help="切回直出（ICP 备案已过时用；默认是切到 302）",
    )
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    to_direct = args.direct

    if not SITE.exists():
        print(f"✗ 找不到 {SITE}", file=sys.stderr)
        return 2

    before = SITE.read_text()
    redirect_blocks, direct_blocks = current_state(before)
    print(f"当前：302 的块 = {redirect_blocks or '（无）'}；直出的块 = {direct_blocks or '（无）'}")

    after, notes = patch_site(before, to_direct)
    target = "直出" if to_direct else "302"
    print(f"切到 {target} 的块：{notes or '（无 —— 已就位或未匹配）'}")
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

    # 切回直出后，redirect 片段就不再被任何块引用 —— 留着会误导下一个人
    # （它仍然装着，是回退开关的实现基础，但不再被 include）。
    if to_direct:
        print("  · redirect 片段仍保留在 snippets/ 里：需要回退时再跑一次本脚本（不加 --direct）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
