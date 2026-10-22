#!/usr/bin/env python3
"""给 nginx 装上**能定位问题**的访问日志格式（幂等）。

## 为什么需要

2026-10-22 实测踩出来的：内测用户反馈「App 下载不了」，服务器日志显示

    GET /downloads/tingxia-0.3.1.apk  200  98304      ← 断了
    GET /downloads/tingxia-0.3.1.apk  200  3836512    ← 同一个文件，同一个 IP，完整下完

两个请求**只差 UA**（小米浏览器 vs 系统下载器）。但当时的日志格式有两个
致命缺口：

1. **不记 Host** —— 这台机器上有 4 个入口（api / www / admin / IP 直连），
   不记 Host 就没法判断「这次请求走的是哪条线路」。而「域名 vs 裸 IP」
   在备案未过的机器上是**完全不同的两件事**：域名走未备案线路，IP 走内网直连。
2. **不记服务端耗时** —— 于是「下载慢」只能猜是服务器慢还是链路慢。
   而真正的原因是客户端浏览器的下载器 —— 猜错方向就改错地方
   （当天先怀疑了用户网络、后来又怀疑了服务器负载，都是被这两个字段缺失
   逼出来的弯路）。

## 装什么

主配置（/etc/nginx/nginx.conf）里加一段 log_format，并把**所有** access_log
指令显式带上格式名（站点块的 access_log 会覆盖 http 块的默认值，
不带格式名就等于没改）。

装到之后，日志行长这样：

    1.2.3.4 - - [22/Oct/2026:21:35:32 +0800] "GET /downloads/x.apk HTTP/2.0" 200 98304
      "https://stashbox.cn/" "Mozilla/5.0 ..." host=stashbox.cn rt=0.031 urt=-

`rt=0.031` 而客户端只拿到 96KB → 服务端 31 毫秒就写完了 → **不是服务器慢**
→ 问题在链路或客户端下载器。这一个字段就能把方向定死。

## 用法

    python3 patch-nginx-logformat.py            # 应用（幂等）
    python3 patch-nginx-logformat.py --check    # 只看会改什么

⚠️ 只改**日志格式**，不碰任何 location / proxy_pass / 证书路径。
改完必须 nginx -t 通过才 reload；不过就保持原状（旧配置继续服务）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

MAIN_CONF = Path("/etc/nginx/nginx.conf")
SITES_DIR = Path("/etc/nginx/sites-enabled")

LOG_FORMAT_NAME = "stashbox"

LOG_FORMAT_BLOCK = f"""
	# ── 访问日志格式（2026-10-22，stashbox 专用）─────────────────────
	#
	# 内置 combined 缺两个关键字段：
	#   · $host —— 这台机器上有 4 个入口（api / www / admin / IP），
	#     不记 Host 就没法判断「这次请求走的是哪条线路」。
	#   · $request_time —— 服务端处理耗时。有了它才能把「服务端慢」
	#     和「链路/客户端慢」分开 —— 这两者的修法完全不同。
	log_format {LOG_FORMAT_NAME}
		'$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent '
		'"$http_referer" "$http_user_agent" '
		'host=$host rt=$request_time urt=$upstream_response_time';
"""


def _nginx_test() -> bool:
    return (
        subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=False).returncode == 0
    )


def patch_main(conf_text: str) -> tuple[str, bool]:
    """在 http 块里加 log_format（已有则跳过）。"""
    if re.search(rf"^\s*log_format\s+{LOG_FORMAT_NAME}\b", conf_text, re.M):
        return conf_text, False
    # 插到 http 块的 access_log 之前（默认 access_log 那行）
    m = re.search(r"^(\s*)access_log\s+/var/log/nginx/access\.log.*$", conf_text, re.M)
    if not m:
        raise SystemExit("主配置里找不到默认 access_log 行，插不进去")
    block = LOG_FORMAT_BLOCK.replace("\t", m.group(1) if m.group(1) else "    ")
    return conf_text[: m.start()] + block + conf_text[m.start() :], True


def patch_access_logs(text: str) -> tuple[str, bool]:
    """给所有 access_log 指令带上格式名（已带的不动）。"""
    pattern = re.compile(r"^(?P<indent>\s*)access_log\s+(?P<path>\S+)\s*;(?P<tail>.*)$", re.M)

    def repl(m: re.Match) -> str:
        tail = m.group("tail")
        if LOG_FORMAT_NAME in tail:
            return m.group(0)
        return f"{m.group('indent')}access_log {m.group('path')} {LOG_FORMAT_NAME};{tail}"

    new_text, count = pattern.subn(repl, text)
    return new_text, count > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看会改什么，不写盘")
    args = ap.parse_args()

    if not MAIN_CONF.exists():
        print(f"✗ 找不到 {MAIN_CONF}", file=sys.stderr)
        return 2

    main_before = MAIN_CONF.read_text()
    main_after, main_changed = patch_main(main_before)

    site_changes: list[tuple[Path, str, str]] = []
    for site in sorted(SITES_DIR.glob("*")):
        if not site.is_file():
            continue
        try:
            before = site.read_text()
        except OSError:
            continue
        after, changed = patch_access_logs(before)
        if changed:
            site_changes.append((site, before, after))

    print(f"主配置 {MAIN_CONF}: {'需要改' if main_changed else '已就位'}")
    for site, _, _ in site_changes:
        print(f"  {site}: access_log 需要加格式名")
    if not site_changes:
        print("  （站点的 access_log 都已带格式名）")

    if args.check:
        return 0

    # 先备份，再写
    stamp = subprocess.run(
        ["date", "+%Y%m%d-%H%M%S"], capture_output=True, text=True, check=False
    ).stdout.strip()
    backup_dir = Path("/etc/nginx/backup")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MAIN_CONF, backup_dir / f"nginx.conf.{stamp}")
    for site, _, _ in site_changes:
        shutil.copy2(site, backup_dir / f"{site.name}.{stamp}")

    MAIN_CONF.write_text(main_after)
    for site, _, after in site_changes:
        site.write_text(after)

    if not _nginx_test():
        # 配置没过 → 回滚，别让服务器起不来
        shutil.copy2(backup_dir / f"nginx.conf.{stamp}", MAIN_CONF)
        for site, _, _ in site_changes:
            shutil.copy2(backup_dir / f"{site.name}.{stamp}", site)
        print("✗ nginx -t 失败，已回滚", file=sys.stderr)
        return 1

    subprocess.run(["systemctl", "reload", "nginx"], check=False)
    print("✓ 已应用并 reload nginx（备份在 /etc/nginx/backup/）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
