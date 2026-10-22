#!/usr/bin/env bash
# 给域名签一张**覆盖全部 host** 的证书。
#
# ## 为什么四个名字必须一张证书
# nginx 的 443 块是**直接写证书路径**的（不走共用片段），四个 host
# 复用同一个证书文件 → 这张证书的 SAN 必须同时含四个名字，
# 否则浏览器按 host 校验会直接报「证书失配」，比拿不到证书更难排查。
#
# ## 为什么不用 certbot
# 这台机器上 certbot 的 /etc/letsencrypt/renewal 目录**根本不存在**
# —— 装过证书但续期配置丢了，那两张证书到期后永远不会续签。
# 现在证书链路只有 acme.sh：签发用 acme.sh，同步+reload 用 renew-tls.sh。
#
# ## 幂等
# 现有证书的 SAN 已覆盖全部名字 → 直接跳过重签
# （不消耗 Let's Encrypt 的速率配额，重复证书配额是 5 张/周）。
#
# ## 用法
#   issue-domain-cert.sh              → 检查 DNS，不通就退出并说明缺哪几条
#   issue-domain-cert.sh --check      → 只检查，不签发
#   issue-domain-cert.sh --wait 60    → 轮询 DNS，最多等 60 分钟，生效后自动签
set -uo pipefail

ACME=/root/.acme.sh/acme.sh
DOMAIN=stashbox.cn
TARGET_IP=8.148.9.151
WEBROOT=/var/www/html
LIVE_DIR=/etc/letsencrypt/live/stashbox.cn
HOSTS=(stashbox.cn www.stashbox.cn admin.stashbox.cn api.stashbox.cn)

MODE=sign
WAIT_MIN=0
# 验证方式：默认 ALPN（走 443）。
# 备案未通过时阿里云会拦**域名**的 80 端口，HTTP-01 根本走不通；
# 而 443 不拦，TLS-ALPN-01 可以完成验证。代价是要独占 443 → 得停 nginx。
VALIDATE=alpn
case "${1:-}" in
  --check)  MODE=check ;;
  --wait)   MODE=sign; WAIT_MIN="${2:-30}" ;;
  --webroot) VALIDATE=webroot ;;
  --alpn)   VALIDATE=alpn ;;
  "")       ;;
  *) echo "用法: $0 [--check | --wait MINUTES] [--alpn|--webroot]"; exit 2 ;;
esac

log() { echo "[$(date +%H:%M:%S)] $*"; }

resolve_ip() {
  # 只看 A 记录 —— AAAA 解析到别处不影响 http-01 验证。
  #
  # 三级回退：系统 resolver → 公共 DNS → 权威 NS。
  # 为什么要回退：系统 resolver 可能还缓存着「记录还没加」时的空结果，
  # 而 Let's Encrypt 的验证器走的是它自己的公网递归 —— 判据必须和
  # 验证器看到的一致，否则会出现「本地说好了、验证器说没好」。
  local ip=""
  ip=$(dig +short "$1" A 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
  [ -n "$ip" ] && { echo "$ip"; return 0; }
  local r
  for r in 8.8.8.8 1.1.1.1; do
    ip=$(dig +short "@$r" "$1" A 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
    [ -n "$ip" ] && { echo "$ip"; return 0; }
  done
  local ns
  for ns in $(dig +short "$DOMAIN" NS 2>/dev/null | head -2); do
    ip=$(dig +short "@$ns" "$1" A 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
    [ -n "$ip" ] && { echo "$ip"; return 0; }
  done
  return 1
}

# 列出当前证书的 SAN（不含端口号）
# openssl 的输出是两行：
#   X509v3 Subject Alternative Name:
#       DNS:stashbox.cn, DNS:www.stashbox.cn
# → 先按逗号切，再剥掉前导空白和 "DNS:" 前缀。
current_sans() {
  [ -f "$LIVE_DIR/fullchain.pem" ] || return 0
  openssl x509 -in "$LIVE_DIR/fullchain.pem" -noout -ext subjectAltName 2>/dev/null \
    | tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -i '^DNS:' \
    | sed 's/^DNS://I' | tr -d ' ' | tr 'A-Z' 'a-z'
}

missing_dns() {
  local bad=()
  for h in "${HOSTS[@]}"; do
    local ip; ip=$(resolve_ip "$h")
    if [ -z "$ip" ]; then
      bad+=("$h 未解析")
    elif [ "$ip" != "$TARGET_IP" ]; then
      bad+=("$h 指向 $ip（应为 $TARGET_IP）")
    fi
  done
  printf '%s\n' "${bad[@]+"${bad[@]}"}" | grep -v '^$'
}

# ── DNS 检查 ────────────────────────────────────────────────────────
check_dns() {
  local bad
  bad=$(missing_dns)
  if [ -z "$bad" ]; then
    log "DNS 全部就绪：$(for h in "${HOSTS[@]}"; do printf '%s→%s ' "$h" "$(resolve_ip "$h")"; done)"
    return 0
  fi
  echo "DNS 未就绪：" >&2
  printf '  ✗ %s\n' "$bad" >&2
  echo >&2
  echo "  需要在阿里云 DNS 里补这些 A 记录（主机记录 / 记录类型 / 记录值）：" >&2
  for h in "${HOSTS[@]}"; do
    ip=$(resolve_ip "$h")
    if [ -z "$ip" ] || [ "$ip" != "$TARGET_IP" ]; then
      echo "    ${h%.$DOMAIN}    A    $TARGET_IP" >&2
    fi
  done
  return 1
}

if [ "$MODE" = "check" ]; then
  check_dns; rc=$?
  echo "当前证书 SAN：$(current_sans | tr '\n' ' ')"
  exit "$rc"
fi

# ── 等待 DNS ───────────────────────────────────────────────────────
if [ "$WAIT_MIN" -gt 0 ]; then
  deadline=$(( $(date +%s) + WAIT_MIN * 60 ))
  log "等待 DNS 生效（最多 $WAIT_MIN 分钟）……"
  while ! check_dns >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "等待超时，DNS 仍未就绪，退出（不改动任何配置）"
      check_dns
      exit 1
    fi
    sleep 15
  done
  log "DNS 已生效"
else
  check_dns || exit 1
fi

# ── 幂等：SAN 已覆盖就跳过重签 ──────────────────────────────────────
sans=$(current_sans)
need=0
for h in "${HOSTS[@]}"; do
  echo "$sans" | grep -qx "$h" || need=1
done
if [ "$need" -eq 0 ]; then
  log "当前证书已覆盖全部 ${#HOSTS[@]} 个名字，跳过重签"
else
  log "当前 SAN：$(echo "$sans" | tr '\n' ' ')"
  log "签发新证书（覆盖 ${#HOSTS[@]} 个名字，验证方式 $VALIDATE）……"
  # --force 是必须的：acme.sh 见到同名证书未到期会直接跳过，
  # 而我们要把 SAN 从 2 个名字扩到 4 个。
  issue_args=(--issue --ecc --server letsencrypt --force
              -d "$DOMAIN" -d "www.$DOMAIN" -d "admin.$DOMAIN" -d "api.$DOMAIN"
              --keylength ec-256)
  if [ "$VALIDATE" = "alpn" ]; then
    # ALPN 验证要独占 443。nginx 占着 → 先停，签完必须起回来
    # （trap 保证脚本中途失败也不会把 nginx 留在停机状态）。
    restore_nginx() { systemctl start nginx >/dev/null 2>&1 || true; }
    trap restore_nginx EXIT
    systemctl stop nginx
    sleep 1
    "$ACME" "${issue_args[@]}" --alpn || {
      echo "签发失败，现有证书与 nginx 配置保持不变" >&2
      systemctl start nginx; trap - EXIT
      exit 1
    }
    systemctl start nginx
    trap - EXIT
  else
    "$ACME" "${issue_args[@]}" -w "$WEBROOT" || {
      echo "签发失败，现有证书与 nginx 配置保持不变" >&2
      exit 1
    }
  fi
fi

# ── 安装到 nginx 读取的路径 ────────────────────────────────────────
mkdir -p "$LIVE_DIR"
# reloadcmd 要兜底：ALPN 签发期间 nginx 是停着的，`systemctl reload nginx`
# 会失败 → acme.sh 退出码非 0 → 把一次**成功**的签发误判成失败。
"$ACME" --install-cert -d "$DOMAIN" --ecc \
  --key-file "$LIVE_DIR/privkey.pem" \
  --fullchain-file "$LIVE_DIR/fullchain.pem" \
  --reloadcmd 'systemctl reload nginx 2>/dev/null || systemctl start nginx' || {
    echo "安装失败" >&2
    exit 1
  }
chmod 644 "$LIVE_DIR/fullchain.pem"; chmod 600 "$LIVE_DIR/privkey.pem"

# ── reload 前先验配置：nginx -t 不过就不 reload，旧证书继续服务 ────
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx
  log "nginx 已 reload"
else
  echo "nginx -t 失败，**未 reload**（旧证书继续服务）" >&2
  nginx -t
  exit 1
fi

# ── 逐 host 验证：SNI 拿到的证书必须含该 host ─────────────────────
echo
echo "=== 逐 host 验证 ==="
fail=0
for h in "${HOSTS[@]}"; do
  san=$(echo | openssl s_client -servername "$h" -connect 127.0.0.1:443 2>/dev/null \
        | openssl x509 -noout -ext subjectAltName 2>/dev/null \
        | tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -i '^DNS:' \
        | sed 's/^DNS://I' | tr -d ' ' | tr 'A-Z' 'a-z')
  if echo "$san" | grep -qx "$h"; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/" 2>/dev/null)
    echo "  ✓ $h  证书匹配  HTTP $code"
  else
    echo "  ✗ $h  证书不匹配（SAN: $(echo "$san" | tr '\n' ' '))"
    fail=1
  fi
done

echo
echo "证书 SAN：$(current_sans | tr '\n' ' ')"
[ "$fail" -eq 0 ] && echo "全部通过" || echo "存在不匹配项"
exit "$fail"
