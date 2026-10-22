#!/usr/bin/env bash
# 证书续期 + 同步 + reload —— 一条 cron 走完。
#
# ## 为什么不能用 acme.sh 自带的 cron
#   1. **80 端口被备案拦截**（域名）→ HTTP-01 验证走不通 → 域名证书只能走
#      TLS-ALPN-01（走 443，不被拦）。ALPN 要**独占 443** → 必须先停 nginx。
#   2. acme.sh --cron **不带 --ecc** 续不了 ECC 证书（我们两张都是 ECC）。
#   3. acme.sh --cron 把两张证书混在一起处理，而两者对 nginx 的要求
#      正好相反：
#        · IP 证书  → HTTP-01 走 http://8.148.9.151/.well-known/...
#                     IP 没有备案概念 → 80 端口**不被拦** → nginx 必须跑着
#        · 域名证书 → ALPN 走 443 → nginx 必须**停着**
#      所以必须分开、按顺序处理。
#
# ## 频率（都是速率配额问题，不是"多久续一次"的问题）
#   · 域名证书 90 天 + ARI 窗口 ~2 天 → 按 conf 的 Le_NextRenewTime
#     提前 3 天进窗口，一天最多续一次。
#   · IP 证书**只有 6 天有效期**，但 Let's Encrypt 对 IP 证书的重复证书
#     配额是**每周 5 张**。每天续一张就是 7 张/周 → 超限。
#     所以卡 4 天续一次（留 2 天余量，一周 1~2 张）。
#
# ## 幂等
# 不在窗口就立刻退出 —— 绝大多数时候 nginx 根本不会被停。
set -uo pipefail

ACME=/root/.acme.sh/acme.sh
ACME_HOME=/root/.acme.sh
DOMAIN=stashbox.cn
IP=8.148.9.151
LOG=/var/log/tls-renew.log

DOMAIN_CONF=$ACME_HOME/${DOMAIN}_ecc/${DOMAIN}.conf
IP_CONF=$ACME_HOME/${IP}_ecc/${IP}.conf
LIVE_DOMAIN=/etc/letsencrypt/live/${DOMAIN}
LIVE_IP=/etc/letsencrypt/live/${IP}

now=$(date +%s)
say() { echo "$(date -Is) $*" >> "$LOG"; echo "$*"; }

# --force：无视窗口判断，强制走一遍续期。
# 用途是**演练** —— 续期链路平时不跑，出事才发现问题就晚了。
# 代价是消耗一张 Let's Encrypt 重复证书配额（每周 5 张）。
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# 读 conf 里的整数项（Le_CertCreateTime / Le_NextRenewTime）
conf_int() {
  local v
  v=$(grep -oP "^$1='\K[0-9]+" "$2" 2>/dev/null | head -1)
  echo "${v:-0}"
}

# ── 0. 先把 acme.sh 的 reloadcmd 换成"兜底式" ─────────────────────
# acme.sh 续期后会自己跑 conf 里记的 reloadcmd。
# 而 ALPN 续期期间 nginx 是**停着**的 → `systemctl reload nginx` 会失败
# → acme.sh 退出码非 0 → 把一次**成功**的续期误判成失败（第一版就踩了这个）。
# 换成「reload 失败就 start」：nginx 跑着走 reload，停着走 start，两种状态都兜住。
RELOAD_CMD='systemctl reload nginx 2>/dev/null || systemctl start nginx'
for d in "$DOMAIN" "$IP"; do
  conf=$ACME_HOME/${d}_ecc/${d}.conf
  [ -f "$conf" ] || continue
  if ! grep -q "^Le_ReloadCmd='$RELOAD_CMD'" "$conf"; then
    "$ACME" --install-cert -d "$d" --ecc \
      --key-file "/etc/letsencrypt/live/$d/privkey.pem" \
      --fullchain-file "/etc/letsencrypt/live/$d/fullchain.pem" \
      --reloadcmd "$RELOAD_CMD" >/dev/null 2>&1 \
      && say "已更新 $d 的 reloadcmd（兜底式）"
  fi
done

# ── 1. IP 证书：4 天一次 ──────────────────────────────────────────
# 必须在 nginx 还跑着的时候做 —— 它的验证方式走 http://IP/.well-known/...
ip_created=$(conf_int Le_CertCreateTime "$IP_CONF")
ip_age_days=0
[ "$ip_created" -gt 0 ] && ip_age_days=$(( (now - ip_created) / 86400 ))
if [ "$ip_age_days" -ge 4 ]; then
  say "IP 证书进入续期窗口（上次签发 ${ip_age_days} 天前）→ HTTP-01 续签"
  if "$ACME" --renew --ecc --force -d "$IP" --server letsencrypt >>"$LOG" 2>&1; then
    say "IP 证书续签成功"
  else
    say "IP 证书续签失败（详见 $LOG）"
  fi
else
  say "IP 证书不在续期窗口（上次签发 ${ip_age_days} 天前，需 ≥4 天），跳过"
fi

# ── 2. 域名证书：窗口内切 ALPN 续期 ───────────────────────────────
domain_next=$(conf_int Le_NextRenewTime "$DOMAIN_CONF")
if [ "$FORCE" -eq 1 ] || { [ "$domain_next" -gt 0 ] && [ $(( now + 3 * 86400 )) -ge "$domain_next" ]; }; then
  say "域名证书进入续期窗口（下次 $domain_next）→ 停 nginx，切 ALPN 续签"
  restore() { systemctl start nginx >/dev/null 2>&1 || true; say "nginx 已恢复运行"; }
  trap restore EXIT
  systemctl stop nginx
  sleep 1
  if "$ACME" --renew --ecc --force -d "$DOMAIN" --server letsencrypt >>"$LOG" 2>&1; then
    say "域名证书续签成功（ALPN）"
  else
    say "域名证书续签失败（详见 $LOG）"
  fi
  systemctl start nginx
  trap - EXIT
else
  say "域名证书不在续期窗口（下次 ${domain_next:-未知}），跳过"
fi

# ── 3. 同步到 nginx 读取的路径 ────────────────────────────────────
sync_cert() {
  local src="$1" dst="$2" keyname="$3"
  [ -f "$src/fullchain.cer" ] || { say "跳过同步：$src/fullchain.cer 不存在"; return 1; }
  mkdir -p "$dst"
  cp "$src/fullchain.cer" "$dst/fullchain.pem"
  cp "$src/$keyname.key" "$dst/privkey.pem"
  chmod 644 "$dst/fullchain.pem"
  chmod 600 "$dst/privkey.pem"
}
sync_cert "$ACME_HOME/${IP}_ecc" "$LIVE_IP" "$IP"
sync_cert "$ACME_HOME/${DOMAIN}_ecc" "$LIVE_DOMAIN" "$DOMAIN"

# IP 兜底块走共用片段（那张是 IP 证书，主机名对不上会证书失配）
if [ -f "$LIVE_IP/fullchain.pem" ]; then
  /opt/stashbox/deploy/tls-cert.sh ip >/dev/null 2>&1 || true
fi

# ── 4. reload 前先验配置 ──────────────────────────────────────────
# nginx 把证书读进内存，不 reload 就还在用旧证书；
# 但 nginx -t 不过时 reload 会让服务起不来 —— 所以先验后 reload。
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx
  say "证书已同步，nginx 已 reload"
else
  say "nginx -t 失败，**未 reload**（旧证书继续服务）"
  nginx -t
fi
