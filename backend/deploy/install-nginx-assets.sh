#!/usr/bin/env bash
#
# 安装 nginx 相关产物（站点配置 + 共享片段 + 可选主配置）
# ==================================================
#
# ## 为什么需要这个脚本
#
# 2026-10-22 走查发现：站点模板 include 了**两个**片段
#
#     include /etc/nginx/snippets/stashbox-downloads.conf;
#     include /etc/nginx/snippets/stashbox-downloads-redirect.conf;
#
# 但全仓库只有 patch-nginx-downloads-redirect.py 会拷**其中一个**，
# 另一个**没有任何脚本会拷**。而 nginx 对 include 一个不存在的文件是
# **emerg**（不是 warning）→ `nginx -t` 直接挂 →
# `&& systemctl reload nginx` 不执行 → **整台机器没有入口**。
#
# 也就是说：严格照 README §9 部署一台新机器，第 ⑨ 步就起不来。
# 这是「代码写了、步骤没跟上」的典型 —— 和 reset-monthly 没进路由表、
# admin 登录不查 deleted_at 是同一个形态。
#
# ## 为什么主配置要单独一个开关
#
# 服务器上的 /etc/nginx/nginx.conf 至今是靠 patch-nginx-logformat.py
# **就地打补丁**维持的（那个脚本往现有文件里插 log_format 和 access_log）。
# 直接覆盖主配置 = 用仓库里的副本盖掉服务器上的真实状态，
# 万一两者有差异就等于把观测能力改没了。
#
# 所以：主配置默认**不装**，要装必须显式 --main（会先备份，
# nginx -t 不过就回滚）。片段和站点配置则每次都装 —— 它们是站点能起来的
# 前提，漏了就 emerg。
#
# ## 用法
#
#     sudo ./install-nginx-assets.sh            # 站点 + 片段（换机必备）
#     sudo ./install-nginx-assets.sh --main     # 额外覆盖主配置（谨慎）
#     ./install-nginx-assets.sh --check         # 只看装没装、差在哪
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/systemd"
SITE_DST="/etc/nginx/sites-available/stashbox"
SITE_LINK="/etc/nginx/sites-enabled/stashbox"
SNIPPET_DIR="/etc/nginx/snippets"
MAIN_DST="/etc/nginx/nginx.conf"
BACKUP_DIR="/etc/nginx/backup"
STAMP="$(date +%Y%m%d-%H%M%S)"

WITH_MAIN=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --main) WITH_MAIN=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }

check_only() {
  say "== 站点配置 =="
  [ -f "$SITE_DST" ] && say "  ✓ $SITE_DST" || say "  ✗ $SITE_DST 缺失"
  [ -L "$SITE_LINK" ] && say "  ✓ $SITE_LINK" || say "  ✗ $SITE_LINK 缺失"
  say "== 共享片段（缺一个 nginx 就 emerg） =="
  for f in stashbox-downloads.conf stashbox-downloads-redirect.conf; do
    [ -f "$SNIPPET_DIR/$f" ] && say "  ✓ $SNIPPET_DIR/$f" || say "  ✗ $SNIPPET_DIR/$f 缺失"
  done
  say "== 主配置 =="
  if [ -f "$MAIN_DST" ]; then
    say "  ✓ $MAIN_DST 存在"
    grep -q 'host=\$host' "$MAIN_DST" && say "    ✓ 含 stashbox 日志格式（host/rt）" \
      || say "    ! 不含 host=/rt= 字段（观测能力缺失，跑 patch-nginx-logformat.py）"
  else
    say "  ✗ $MAIN_DST 缺失"
  fi
}

if [ "$CHECK_ONLY" = 1 ]; then
  check_only
  exit 0
fi

if [ "$(id -u)" != 0 ]; then
  say "需要 root：sudo $0" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

backup() {
  local f="$1"
  [ -f "$f" ] || return 0
  cp -a "$f" "$BACKUP_DIR/$(basename "$f").$STAMP.bak"
  say "  · 已备份 $f → $BACKUP_DIR/$(basename "$f").$STAMP.bak"
}

install_file() {
  local src="$1" dst="$2"
  if [ ! -f "$src" ]; then
    say "  ✗ 源文件不存在: $src" >&2
    return 1
  fi
  backup "$dst"
  install -m 644 "$src" "$dst"
  say "  ✓ $dst"
}

say "== 装站点配置 =="
install_file "$SRC_DIR/nginx.conf" "$SITE_DST" || exit 1
mkdir -p "$(dirname "$SITE_LINK")"
ln -sf "$SITE_DST" "$SITE_LINK"
say "  ✓ $SITE_LINK"

say "== 装共享片段 =="
mkdir -p "$SNIPPET_DIR"
install_file "$SRC_DIR/snippets/stashbox-downloads.conf" "$SNIPPET_DIR/stashbox-downloads.conf" || exit 1
install_file "$SRC_DIR/snippets/stashbox-downloads-redirect.conf" "$SNIPPET_DIR/stashbox-downloads-redirect.conf" || exit 1

if [ "$WITH_MAIN" = 1 ]; then
  say "== 装主配置（--main） =="
  install_file "$SRC_DIR/nginx-main.conf" "$MAIN_DST" || exit 1
else
  say "== 主配置：跳过（要装加 --main；服务器上现由 patch-nginx-logformat.py 维护） =="
fi

say "== nginx -t =="
if ! nginx -t; then
  say "✗ nginx -t 没过 —— 已安装的文件留在原地，备份在 $BACKUP_DIR" >&2
  say "  排查：ls $BACKUP_DIR/*.$STAMP.bak" >&2
  exit 1
fi

systemctl reload nginx
say "✓ nginx 已 reload"
