#!/usr/bin/env python3
"""把 nginx 配置里关于证书的**过时注释**更新成实际口径。

为什么要改：注释里写着「certbot 签的域名证书（90 天，自动续签）」，
但服务器上 certbot 的 /etc/letsencrypt/renewal 目录**根本不存在**
—— 装过证书、续期配置丢了，那两张证书到期即死。实际的证书链路是
acme.sh + 自建续期脚本。注释不跟上，会让人照着跑 certbot 再踩一次。

另外「备案期间域名会被阿里云监测系统阻断」也写得太笼统：
实测只拦**域名的 80 端口**，443 正常、IP 的 80 也正常。
这条差别直接决定「域名证书能不能签」和「方能不能访问」是两件事。

幂等：同一段替换已经做过就跳过（不会重复套用）。
"""

from __future__ import annotations

import pathlib
import sys

# (旧文本, 新文本, 期望出现次数)
REPLACEMENTS: list[tuple[str, str, int]] = [
    (
        """#   证书用 certbot 申请（阿里云 DNS 验证或 HTTP-01 均可）：
#       certbot --nginx -d api.stashbox.cn
#   certbot 会自己改这个文件；改完记得 nginx -t。""",
        """#   证书用 acme.sh 签（这台机器的 certbot 续期目录是坏的，别用）：
#       bash /opt/stashbox/deploy/issue-domain-cert.sh
#   ⚠️ 备案期间**域名的 80 端口**被阿里云拦（HTTP-01 走不通）→
#     域名证书实际走 TLS-ALPN-01（443）；IP 证书照旧走 HTTP-01。""",
        1,
    ),
    (
        """#   A. **域名已备案** → 用 certbot 给域名签的证书（90 天，自动续签）
#      /etc/letsencrypt/live/api.stashbox.cn/{fullchain,privkey}.pem
#
#   B. **域名还在备案中** → 用 Let's Encrypt 签的 **IP 证书**（6 天，自动续签）
#      /etc/letsencrypt/live/8.148.9.151/{fullchain,privkey}.pem
#      （Let's Encrypt 自 2025-12 起支持 IP 证书，http-01 验证）
#
# nginx 的 ssl_certificate **不能**用变量（除非 1.15+ 的变量展开，
# 这里为了可读性直接用 map 生成路径 —— 实际是 include 一个片段文件）。
#
# 做法：把选中的路径写进 /etc/nginx/snippets/tls-cert.conf，
# 三个 server 块统一 include 它。切换证书只需重写这个片段 + reload。""",
        """#   · 域名证书（4 个名字，90 天）→ 域名块（公开面 / API / 后台）
#     /etc/letsencrypt/live/stashbox.cn/{fullchain,privkey}.pem
#     由 acme.sh 签发并由 deploy/renew-tls.sh 自动续期。
#     **备案期间域名的 80 端口被阿里云拦** → HTTP-01 走不通
#     → 实际走 TLS-ALPN-01（验证走 443，443 不拦）。
#
#   · IP 证书（6 天）→ IP 兜底块（server_name _）
#     /etc/letsencrypt/live/8.148.9.151/{fullchain,privkey}.pem
#     HTTP-01 验证。IP 没有备案概念 → 80 端口**不被拦**。
#     续期卡到 4 天一次（LE 对 IP 证书的重复证书配额是每周 5 张）。
#
# 两张证书的 SAN 不重叠 → 域名块**直接写证书路径**，不走共用片段；
# IP 兜底块才 include /etc/nginx/snippets/tls-cert.conf。""",
        1,
    ),
    (
        "    # certbot 的 HTTP-01 续签要走这个路径，别把它也跳转了。",
        "    # ACME HTTP-01 验证要走这个路径，别把它也跳转了。",
        1,
    ),
    (
        "    # certbot HTTP-01 续签路径",
        "    # ACME HTTP-01 验证路径",
        1,
    ),
    (
        "    # 证书路径换成 certbot 实际签发的那一对。",
        "    # 证书路径换成 acme.sh 实际签发的那一对。",
        1,
    ),
    (
        "    # 复用 admin 的证书（自签或 certbot 签的真实证书都行）",
        "    # IP 兜底入口用 **IP 证书**（IP 入口与域名入口主机名不同，必须各用各的）",
        1,
    ),
    (
        """# ⚠️ 证书：certbot 一次签两个域名最省事：
#    certbot --nginx -d api.stashbox.cn -d admin.stashbox.cn
#    或者分别签。改完记得 nginx -t。""",
        """# ⚠️ 证书：四个名字**一张证书**（它们复用同一个证书文件，
#    SAN 少一个就报证书失配）：
#    bash /opt/stashbox/deploy/issue-domain-cert.sh
#    备案期间会临时停 nginx（ALPN 要独占 443），签完自动起回来。""",
        1,
    ),
    (
        "    # 域名块用**域名证书**（SAN: stashbox.cn / www.stashbox.cn）——",
        "    # 域名块用**域名证书**（SAN: stashbox.cn / www. / admin. / api. stashbox.cn）——",
        3,
    ),
    (
        "    # ── TLS（域名证书，SAN: stashbox.cn / www.stashbox.cn）────────────",
        "    # ── TLS（域名证书，SAN: stashbox.cn / www. / admin. / api. stashbox.cn）──",
        1,
    ),
]


def patch(path: pathlib.Path) -> int:
    text = path.read_text()
    changed = 0
    for old, new, expect in REPLACEMENTS:
        found = text.count(old)
        if found == 0:
            # 已经改过了（或本来就没有）—— 不算失败
            continue
        if found != expect:
            print(
                f"  ⚠️ {path}: 「{old.strip()[:48]}…」出现 {found} 次，预期 {expect} 次，跳过",
                file=sys.stderr,
            )
            continue
        text = text.replace(old, new)
        changed += found
        print(f"  ✓ {path}: 替换 {found} 处「{old.strip()[:40]}…」")
    if changed:
        path.write_text(text)
    return changed


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: patch-nginx-tls-comments.py <nginx 配置路径> [...]", file=sys.stderr)
        return 2
    total = 0
    for arg in sys.argv[1:]:
        p = pathlib.Path(arg)
        if not p.exists():
            print(f"  ✗ 不存在：{p}", file=sys.stderr)
            continue
        total += patch(p)
    print(f"共替换 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
