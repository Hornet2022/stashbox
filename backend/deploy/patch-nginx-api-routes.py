#!/usr/bin/env python3
"""补齐 nginx 里 `api.` 块与 IP 兜底块缺失的转发规则。

## 为什么

2026-10-22 实测发现两个洞（都是「域名切过去才发现」的那种）：

1. **`api.stashbox.cn` 没有 `location /api/`** —— 它只转发了
   `location = /api/v1/auth/token`（精确匹配一条）和 `location /audio/`。
   也就是说 `https://api.stashbox.cn/api/v1/articles` 会落到 nginx 的
   默认 root → **404**。这个域名义上是"App 的 API 入口"，
   但照现在的配置，App 打过去**全线 404**。
   公开面块（`stashbox.cn`）是有 `location /api/` 的，api 块漏抄了。

2. **IP 兜底块的 `/healthz` 被 SPA 吞了** —— `location / { try_files ... /index.html; }`
   会把 `/healthz` 变成后台 SPA 的 HTML，**状态码还是 200**。
   任何拿它当探针的监控/负载均衡都会拿到「200 + HTML」，
   验内容就会误判不健康、只验状态码就会误判健康。
   公开面块已经用 `location ~ ^/(healthz|readyz|health|metrics)$` 修过，
   IP 块漏了。

## 幂等

按 server 块定位（按 `server_name`），块里已有对应 location 就跳过。
"""

from __future__ import annotations

import pathlib
import re
import sys

API_ROUTES = """    location /api/ {
        proxy_pass http://stashbox_gateway;
    }

    location ~ ^/(healthz|readyz|health|metrics)$ {
        proxy_pass http://stashbox_gateway;
    }

"""

HEALTH_ROUTE = """    location ~ ^/(healthz|readyz|health|metrics)$ {
        proxy_pass http://stashbox_gateway;
    }

"""


def server_block(text: str, server_name: str) -> tuple[int, int] | None:
    """返回 server_name 所在 **443** server 块的 (块首, 块尾) 偏移。

    ⚠️ 必须限定 443：同一个 server_name 通常有**两个**块 ——
    80 端口那个只做 ACME 路径 + 301 跳转，443 那个才是真正干活的。
    只按 server_name 匹配会抓到 80 端口那个（它没有 /api/ 转发）。
    """
    m = re.search(
        rf"^server\s*\{{(?:(?!^\}}).)*?listen\s+443(?:(?!^\}}).)*?"
        rf"server_name\s+[^;]*\b{re.escape(server_name)}\b[^;]*;(?:(?!^\}}).)*?^\}}",
        text,
        re.S | re.M,
    )
    return (m.start(), m.end()) if m else None


def patch(path: pathlib.Path) -> int:
    text = path.read_text()
    changed = 0

    # ── api 块：补 /api/ 与健康检查 ──────────────────────────────
    span = server_block(text, "api.stashbox.cn")
    if span is None:
        print(f"  ✗ {path}: 找不到 server_name api.stashbox.cn 的块", file=sys.stderr)
    else:
        s, e = span
        block = text[s:e]
        if "location /api/" in block:
            print(f"  · {path}: api 块已有 location /api/，跳过")
        else:
            anchor = "    location /audio/ {"
            if anchor not in block:
                print(f"  ✗ {path}: api 块里找不到锚点 {anchor!r}", file=sys.stderr)
            else:
                block = block.replace(anchor, API_ROUTES + anchor, 1)
                text = text[:s] + block + text[e:]
                changed += 1
                print(f"  ✓ {path}: api 块补上 location /api/ + 健康检查")

    # ── IP 兜底块：补健康检查 ───────────────────────────────────
    span = server_block(text, "8.148.9.151")
    if span is None:
        # default_server 块的 server_name 是 _，用 server_name _ 兜一次
        span = server_block(text, "_")
    if span is None:
        print(f"  ✗ {path}: 找不到 IP 兜底块", file=sys.stderr)
    else:
        s, e = span
        block = text[s:e]
        if "location ~ ^/(healthz|readyz|health|metrics)$" in block:
            print(f"  · {path}: IP 兜底块已有健康检查 location，跳过")
        else:
            anchor = "    location / {"
            if anchor not in block:
                print(f"  ✗ {path}: IP 兜底块里找不到锚点 {anchor!r}", file=sys.stderr)
            else:
                block = block.replace(anchor, HEALTH_ROUTE + anchor, 1)
                text = text[:s] + block + text[e:]
                changed += 1
                print(f"  ✓ {path}: IP 兜底块补上健康检查 location")

    if changed:
        path.write_text(text)
    return changed


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: patch-nginx-api-routes.py <nginx 配置路径> [...]", file=sys.stderr)
        return 2
    total = 0
    for arg in sys.argv[1:]:
        p = pathlib.Path(arg)
        if not p.exists():
            print(f"  ✗ 不存在：{p}", file=sys.stderr)
            continue
        total += patch(p)
    print(f"共修改 {total} 个块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
