#!/usr/bin/env bash
# 续签证书 → 同步到 letsencrypt 目录 → 重写 nginx 片段 → reload
#
# 两张证书，各自的 SAN 不重叠，所以**必须**各走各的：
#   · IP 证书  → IP 兜底块（server_name _）—— 内测回退路径
#   · 域名证书 → API 块 + 后台块
# acme.sh 的 cron（0 3 */3 * *）负责**签发**，这个脚本负责**同步+reload**
# （nginx 把证书读进内存，不 reload 就还在用旧证书）。
set -uo pipefail

DOMAIN_SRC=/root/.acme.sh/stashbox.cn_ecc
DOMAIN_DST=/etc/letsencrypt/live/stashbox.cn
IP_SRC=/root/.acme.sh/8.148.9.151_ecc
IP_DST=/etc/letsencrypt/live/8.148.9.151

# ── IP 证书 ──────────────────────────────────────────────────
if [ -f "$IP_SRC/fullchain.cer" ]; then
  cp "$IP_SRC/fullchain.cer" "$IP_DST/fullchain.pem"
  cp "$IP_SRC/8.148.9.151.key" "$IP_DST/privkey.pem"
  chmod 644 "$IP_DST/fullchain.pem"; chmod 600 "$IP_DST/privkey.pem"
  /opt/stashbox/deploy/tls-cert.sh ip >/dev/null 2>&1
  echo "$(date -Is) IP 证书已续签并 reload nginx" >> /var/log/tls-renew.log
fi

# ── 域名证书 ────────────────────────────────────────────────
# ⚠️ 域名块在 nginx 里是**直接写路径**（不走共用片段）——
#    共用片段那张是 IP 证书，主机名对不上会证书失配。
if [ -f "$DOMAIN_SRC/fullchain.cer" ]; then
  mkdir -p "$DOMAIN_DST"
  cp "$DOMAIN_SRC/fullchain.cer" "$DOMAIN_DST/fullchain.pem"
  cp "$DOMAIN_SRC/stashbox.cn.key" "$DOMAIN_DST/privkey.pem"
  chmod 644 "$DOMAIN_DST/fullchain.pem"; chmod 600 "$DOMAIN_DST/privkey.pem"
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    echo "$(date -Is) 域名证书已续签并 reload nginx" >> /var/log/tls-renew.log
  else
    echo "$(date -Is) 域名证书同步后 nginx -t 失败，**未 reload**（旧证书继续服务）" \
      >> /var/log/tls-renew.log
  fi
fi
