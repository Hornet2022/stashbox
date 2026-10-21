#!/usr/bin/env bash
#
# TLS 证书选择 —— 决定 nginx 用哪张证书
# ================================
#
# ## 为什么需要这个脚本
#
# 备案期间域名解析会被阿里云监测系统阻断，而安卓 release 包
# **只信任系统 CA**（network_security_config.xml 的 base-config
# cleartextTrafficPermitted=false + trust-anchors=system），
# 自签证书一律被拒 —— 也就是说：
#
#   · 用域名 → 备案通过前，App 连不上
#   · 用自签证书 → 任何时候都连不上
#
# 解法：Let's Encrypt 自 2025-12 起支持**签发 IP 证书**（http-01 验证，
# 6 天有效期，自动续签）。所以备案期间可以用 IP 证书，
# 备案通过后换成域名证书 —— 同一个 nginx 配置，两种证书。
#
# ## 用法
#
#     ./tls-cert.sh ip                 # 用 IP 证书（备案期间）
#     ./tls-cert.sh domain api.stashbox.cn   # 用域名证书（备案通过后）
#     ./tls-cert.sh auto               # 有域名证书就用域名，否则回退 IP
#     ./tls-cert.sh status             # 只看当前用的哪张
#
# 切换后会自动 nginx -t + reload（失败则回滚）。
set -uo pipefail

SNIPPET_DIR=/etc/nginx/snippets
SNIPPET="$SNIPPET_DIR/tls-cert.conf"
DOMAIN_BASE=/etc/letsencrypt/live
IP_CERT_DIR="$DOMAIN_BASE/8.148.9.151"

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

write_snippet() {
  local cert="$1" key="$2" desc="$3"
  mkdir -p "$SNIPPET_DIR"
  cat > "$SNIPPET" <<EOF
# 由 tls-cert.sh 生成 —— $desc
# 切换证书请用 tls-cert.sh，不要手改这个文件。
ssl_certificate     $cert;
ssl_certificate_key $key;
EOF
  chmod 644 "$SNIPPET"
}

apply_and_reload() {
  local desc="$1"
  if ! nginx -t 2>/tmp/nginx_t.err; then
    echo "✗ nginx -t 失败，已回滚" >&2
    cat /tmp/nginx_t.err >&2
    if [ -f "$SNIPPET.bak" ]; then
      mv "$SNIPPET.bak" "$SNIPPET"
      nginx -t >/dev/null 2>&1 && systemctl reload nginx
    fi
    exit 1
  fi
  rm -f "$SNIPPET.bak"
  systemctl reload nginx
  echo "✓ 已切换到 $desc 并 reload nginx"
}

use_ip() {
  [ -f "$IP_CERT_DIR/fullchain.pem" ] && [ -f "$IP_CERT_DIR/privkey.pem" ] || {
    echo "✗ 没有 IP 证书：$IP_CERT_DIR（先跑 acme.sh 签发）" >&2; exit 1; }
  [ -f "$SNIPPET" ] && cp "$SNIPPET" "$SNIPPET.bak"
  write_snippet "$IP_CERT_DIR/fullchain.pem" "$IP_CERT_DIR/privkey.pem" "IP 证书 8.148.9.151（Let's Encrypt，6 天自动续签）"
  apply_and_reload "IP 证书（8.148.9.151）"
}

use_domain() {
  local d="$1"
  [ -f "$DOMAIN_BASE/$d/fullchain.pem" ] && [ -f "$DOMAIN_BASE/$d/privkey.pem" ] || {
    echo "✗ 没有域名证书：$DOMAIN_BASE/$d" >&2; exit 1; }
  [ -f "$SNIPPET" ] && cp "$SNIPPET" "$SNIPPET.bak"
  write_snippet "$DOMAIN_BASE/$d/fullchain.pem" "$DOMAIN_BASE/$d/privkey.pem" "域名证书 $d（certbot，90 天自动续签）"
  apply_and_reload "域名证书（$d）"
}

status() {
  if [ -f "$SNIPPET" ]; then
    echo "当前配置："
    sed 's/^/  /' "$SNIPPET"
  else
    echo "  （还没有 $SNIPPET）"
  fi
  echo
  echo "可用的证书："
  for d in "$DOMAIN_BASE"/*/; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    if [ -f "$d/fullchain.pem" ]; then
      exp=$(openssl x509 -in "$d/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
      san=$(openssl x509 -in "$d/fullchain.pem" -noout -ext subjectAltName 2>/dev/null | tail -1 | tr -s ' ')
      echo "  $n  到期=$exp  $san"
    fi
  done
}

case "${1:-auto}" in
  ip) use_ip ;;
  domain)
    [ $# -ge 2 ] || usage
    use_domain "$2" ;;
  auto)
    # 有域名证书优先用域名（长期方案），否则 IP
    if compgen -G "$DOMAIN_BASE/*/fullchain.pem" >/dev/null 2>&1; then
      d=$(basename "$(dirname "$(ls -1 "$DOMAIN_BASE"/*/fullchain.pem 2>/dev/null | head -1)")")
      use_domain "$d"
    else
      use_ip
    fi ;;
  status) status ;;
  *) usage ;;
esac
