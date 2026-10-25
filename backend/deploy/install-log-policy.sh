#!/usr/bin/env bash
# 听匣生产服务器：日志轮转策略（按天 + 按大小 + 压缩归档）
#
# ══════════════════════════════════════════════════════════════════════
# 为什么要单独一个脚本
# ══════════════════════════════════════════════════════════════════════
# 日志策略散在两处就会漂：
#   · /etc/logrotate.d/stashbox（我们的文件日志）
#   · /etc/logrotate.d/nginx（系统的 nginx 日志策略，**它也管我们的 4 个 vhost**）
# 而 journald 那一半在 tune-host.sh 里（journald.conf）。
#
# 本脚本是 **logrotate 那一半的唯一真相源**：
#   · 装 /etc/logrotate.d/stashbox
#   · 给 /etc/logrotate.d/nginx 补「按大小」这一维
#   · 建日志目录（缺目录 → nginx 起不来，日志直接写不进去）
# 可重复执行（幂等）、可 --dry-run。
#
# ══════════════════════════════════════════════════════════════════════
# 三层日志，各管各的
# ══════════════════════════════════════════════════════════════════════
# ① 应用日志（四个 uvicorn 服务 + arq worker）→ **journald**
#    systemd unit 里 StandardOutput=journal。
#    限容在 /etc/systemd/journald.conf（tune-host.sh 写）：
#       SystemMaxUse=200M      总量上限
#       SystemMaxFileSize=50M  单文件上限
#       SystemKeepFree=512M    无论如何给盘留 512M
#       MaxRetentionSec=30day  最多留 30 天
#       Storage=persistent      落盘（重启后还在，排查问题要用）
#    journal 本身是 zstd 压缩存储 → 「压缩归档」这一层 journald 自带。
#    查：journalctl -u stashbox-ai-worker -f
#
# ② nginx 访问/错误日志 → /var/log/nginx/*.log
#    系统自带 /etc/logrotate.d/nginx（daily + rotate 14 + compress）。
#    **缺「按大小」**：只有天级轮转，撞上爬虫猛打 /downloads（APK 就在那儿），
#    一天就能写几百 MB，14 份归档全留着 → 40G 盘被日志吃光。
#    本脚本给它补 maxsize（见下面）。
#
# ③ 我们自己的文件日志 → /var/log/stashbox/*.log
#    现在是空的（应用走 journald），但策略留着：哪天有服务要写文件日志，
#    不用再临时想轮转规则。
#
# ══════════════════════════════════════════════════════════════════════
# 两条硬规矩
# ══════════════════════════════════════════════════════════════════════
# · **不能给同一个文件写两条 logrotate 规则** —— logrotate 会报
#   "duplicate log entry" 并**整轮放弃**（nginx 的日志就全都不轮转了）。
#   所以「按大小」是**加进系统已有的 nginx 那一段**，不是另起一段。
# · **copytruncate**：服务持有日志文件的 fd，单纯 rename 的话它会继续往
#   已改名的文件里写（老文件继续长、新的从 0 开始）—— 那就等于没轮转。
#   copytruncate 是「复制内容 + 截断原文件」，fd 不用动。
set -euo pipefail

DRY_RUN=0
LOG_DIR="/var/log/stashbox"
LOGROTATE_OURS="/etc/logrotate.d/stashbox"
LOGROTATE_NGINX="/etc/logrotate.d/nginx"
NGINX_MAXSIZE="100M"
OURS_MAXSIZE="50M"
OURS_ROTATE="14"
SERVICE_USER="stashbox"
SERVICE_GROUP="stashbox"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "用法: $0 [--dry-run]" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }

# ── 备份目录限量 ───────────────────────────────────────────────────────
#
# 2026-10-25：备份目录原先是 `/root/nginx-config-backups/`（一次性堆到 480K），
# 而 README 写明的规范位置是 `/etc/nginx/backup/` —— 两处不一致，已归位。
#
# ⚠️ **归位还不够**：这个脚本每跑一次就多一份 `.bak`，所以必须限量。
# 之前 `/root` 下 17 个 `*backup*` 目录、797MB 就是这么攒出来的
# （其中 `/root/.cache/ms-playwright` 一家就占 658M）——
# 「备份」如果没有淘汰机制，攒着攒着就比它要保护的东西还大。
#
# 保留最近 5 份：够回滚最近几次改动，又不会无限长。
BACKUP_DIR="${BACKUP_DIR:-/etc/nginx/backup}"
BACKUP_KEEP="${BACKUP_KEEP:-5}"

# 备份目录按 mtime 从新到旧排，只留最近 N 份。
prune_backups() {
  local dir="${1:-$BACKUP_DIR}" keep="${2:-$BACKUP_KEEP}"
  [ -d "$dir" ] || return 0
  ls -1t "$dir" 2>/dev/null | tail -n "+$((keep + 1))" | while IFS= read -r f; do
    [ -n "$f" ] && rm -f -- "$dir/$f"
  done
}
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

[ "$(id -u)" -eq 0 ] || { echo "需要 root" >&2; exit 1; }

# ── 1. 日志目录 ──────────────────────────────────────────────────────
# 这是**服务用户**的文件日志目录（应用日志实际走 journald，这里是兜底）。
# 归属跟 tune-host.sh 建的保持一致（stashbox:stashbox 0750）。
#
# ⚠️ nginx 的日志**不在这里** —— 它们留在 /var/log/nginx/，由系统的
# /etc/logrotate.d/nginx 管（下面只给它补「按大小」这一维）。
# 两边混住会踩 logrotate 的 duplicate log entry：同一个文件被两段规则管到，
# logrotate 会整轮放弃（nginx 的日志就全都不轮转了）。
say "日志目录 $LOG_DIR"
if [ ! -d "$LOG_DIR" ]; then
  run mkdir -p "$LOG_DIR"
fi
if id "$SERVICE_USER" >/dev/null 2>&1; then
  run chown "${SERVICE_USER}:${SERVICE_GROUP}" "$LOG_DIR"
  run chmod 0750 "$LOG_DIR"
else
  say "  ⚠️ 用户 $SERVICE_USER 不存在 —— 目录归属没改（服务用户建好后重跑本脚本）"
fi

# ── 2. 我们自己的文件日志策略 ────────────────────────────────────────
say "logrotate 策略 $LOGROTATE_OURS"
mkdir -p /etc/nginx/backup
cat > /tmp/stashbox-logrotate.$$ <<EOF
# 听匣自己的文件日志（应用日志实际走 journald，这里是给文件日志兜底）。
# 由 backend/deploy/install-log-policy.sh 生成 —— 别手改，改脚本。
#
# daily      按天轮转
# maxsize    单个文件超过这个大小也立刻轮转（**天级之外的第二道闸**）：
#            撞上爬虫猛打 /downloads（APK 就在那儿），一天能写几百 MB。
# rotate     保留 14 份归档
# compress   归档压缩（gz）
# delaycompress 最近一份不压缩 —— 出事时能直接 tail，不用先 gunzip
# copytruncate 服务持有 fd，不能只改名（见脚本头「两条硬规矩」）
$LOG_DIR/*.log {
    daily
    maxsize $OURS_MAXSIZE
    rotate $OURS_ROTATE
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0640 $SERVICE_USER $SERVICE_GROUP
}
EOF
if [ "$DRY_RUN" -eq 1 ]; then
  say "    [dry-run] 写 $LOGROTATE_OURS"
else
  if [ -f "$LOGROTATE_OURS" ] && ! cmp -s /tmp/stashbox-logrotate.$$ "$LOGROTATE_OURS"; then
    cp -a "$LOGROTATE_OURS" "/etc/nginx/backup/$(basename "$LOGROTATE_OURS").bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  fi
  install -m 0644 /tmp/stashbox-logrotate.$$ "$LOGROTATE_OURS"
fi
rm -f /tmp/stashbox-logrotate.$$

# ── 3. 给系统 nginx 策略补「按大小」 ──────────────────────────────────
say "logrotate 策略 $LOGROTATE_NGINX（补 maxsize）"
if [ ! -f "$LOGROTATE_NGINX" ]; then
  say "  ⚠️ $LOGROTATE_NGINX 不存在（nginx 装了吗？）—— 跳过"
elif grep -qE "^\s*maxsize\s" "$LOGROTATE_NGINX"; then
  say "  已有 maxsize，跳过"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    say "    [dry-run] 在 nginx 策略里补 maxsize $NGINX_MAXSIZE"
  else
    mkdir -p /etc/nginx/backup
    cp -a "$LOGROTATE_NGINX" "/etc/nginx/backup/$(basename "$LOGROTATE_NGINX").bak.$(date +%Y%m%d%H%M%S)"
    # 在第一段（/var/log/nginx/*.log）里、daily 之后插一行 maxsize。
    #
    # 用 awk 而不是 sed：BSD sed（macOS）不支持替换串里的 \n，
    # GNU sed 支持 —— 同一份脚本在两边跑会有一边炸。awk 两边都稳。
    awk -v ins="	maxsize $NGINX_MAXSIZE" '
      !done && /^[[:space:]]*daily[[:space:]]*$/ { print; print ins; done=1; next }
      { print }
    ' "$LOGROTATE_NGINX" > /tmp/nginx-logrotate.$$ && cat /tmp/nginx-logrotate.$$ > "$LOGROTATE_NGINX"
    rm -f /tmp/nginx-logrotate.$$
    say "  ✓ 已补 maxsize $NGINX_MAXSIZE（备份在 /etc/nginx/backup/）"
  fi
fi

# ── 4. 校验 ──────────────────────────────────────────────────────────
say "校验"
if [ "$DRY_RUN" -eq 0 ]; then
  if logrotate -d "$LOGROTATE_OURS" > /tmp/logrotate-check.out 2>&1; then
    say "  ✓ logrotate -d $LOGROTATE_OURS 通过"
  else
    say "  ✗ logrotate -d 失败："
    tail -20 /tmp/logrotate-check.out >&2
    exit 1
  fi
  logrotate -d "$LOGROTATE_NGINX" > /tmp/logrotate-check-nginx.out 2>&1 || {
    say "  ✗ nginx 那段校验失败："
    tail -20 /tmp/logrotate-check-nginx.out >&2
    exit 1
  }
  say "  ✓ logrotate -d $LOGROTATE_NGINX 通过"
  journalctl --disk-usage | sed 's/^/  /'
fi

say "完成。"
# 备份目录限量：只留最近 N 份（少了它，备份目录会自己长成垃圾堆 —— 见 prune_backups 的注释）
prune_backups
say "  备份目录 $BACKUP_DIR：保留最近 ${BACKUP_KEEP} 份"
cat <<'EOF'
回滚：
  rm -f /etc/logrotate.d/stashbox
  # nginx 那段：从 /etc/nginx/backup/nginx.bak.* 恢复
EOF
