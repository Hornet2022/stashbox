"""把 nginx 切成「域名块用域名证书、IP 块留 IP 证书」。

## 为什么必须分

三个 server 块（API / IP 兜底 / 后台）**共用** `/etc/nginx/snippets/tls-cert.conf`
—— 一张证书切片。而：
  · 域名证书的 SAN = stashbox.cn / www.stashbox.cn（**不含 IP**）
  · IP 证书的 SAN = IP:8.148.9.151（**不含域名**）

所以共用一张必然有一边失配：
  · 全用域名证书 → **App 现在的 IP 入口直接连不上**（证书不匹配）
  · 全用 IP 证书   → 域名入口失配

切成两块各用各的：IP 兜底块继续用 IP 证书（**内测回退路径不受影响**），
API 块和后台块用域名证书。
"""

import re
import shutil
import sys
from pathlib import Path

NGINX = Path("/etc/nginx/sites-enabled/stashbox")
BAK = Path("/etc/nginx/sites-enabled/stashbox.bak-domain")
DOMAIN_SNIPPET = "/etc/nginx/snippets/tls-cert-domain.conf"
DOMAIN_DIR = "/etc/letsencrypt/live/stashbox.cn"

IP_SNIPPET = "/etc/nginx/snippets/tls-cert.conf"


def main() -> int:
    src = NGINX.read_text(encoding="utf-8")
    if BAK.exists():
        print(f"  已有备份 {BAK}，不重复备份")
    else:
        shutil.copy2(NGINX, BAK)
        print(f"  已备份 → {BAK}")

    # ── 1) server_name 补域名 ──────────────────────────────────
    # API 块：api.stashbox.cn + stashbox.cn
    # 后台块：admin.stashbox.cn + www.stashbox.cn
    # :80 的重定向块也补（否则 http 访问新域名会落到 default_server）
    def add_name(text: str, old: str, new: str) -> str:
        return text.replace(f"server_name {old};", f"server_name {new};")

    src = add_name(src, "api.stashbox.cn", "api.stashbox.cn stashbox.cn")
    src = add_name(src, "admin.stashbox.cn", "admin.stashbox.cn www.stashbox.cn")

    # ── 2) API 块与后台块改用域名证书 ─────────────────────────
    # 用「server_name 行 + 其后的 include」定位，避免动到 IP 兜底块
    out_lines = []
    lines = src.splitlines(keepends=True)
    current_block = None
    for ln in lines:
        m = re.search(r"server_name\s+([^;]+);", ln)
        if m:
            current_block = m.group(1)
        if "include /etc/nginx/snippets/tls-cert.conf;" in ln:
            is_ip_block = current_block is not None and current_block.strip() == "_"
            if not is_ip_block:
                ln = (
                    f"    # 域名块用**域名证书**（SAN: stashbox.cn / www.stashbox.cn）——\n"
                    f"    # 不能用共用片段：那张是 IP 证书，跟这里的主机名对不上。\n"
                    f"    ssl_certificate     {DOMAIN_DIR}/fullchain.pem;\n"
                    f"    ssl_certificate_key {DOMAIN_DIR}/privkey.pem;\n"
                )
        out_lines.append(ln)
    src = "".join(out_lines)

    NGINX.write_text(src, encoding="utf-8")
    print("  nginx 配置已改")
    return 0


if __name__ == "__main__":
    sys.exit(main())
