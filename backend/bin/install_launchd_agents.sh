#!/usr/bin/env bash
# install_launchd_agents.sh — 安装/卸载 听匣后端 4 服务 + arq worker 的 launchd agent。
#
# 用法：
#   bin/install_launchd_agents.sh install    # 安装（plist 已在 ~/Library/LaunchAgents/，本脚本 load）
#   bin/install_launchd_agents.sh uninstall  # 卸载（unload + 删 plist）
#   bin/install_launchd_agents.sh status     # 看哪些已 load
#
# 设计要点：
# - plist 已写在 ~/Library/LaunchAgents/com.stashbox.{api-gateway,user-service,content-service,ai-service,ai-worker}.plist
#   （本脚本不动 plist，只 load/unload 它们）
# - KeepAlive=true：服务崩了 launchd 自动重启
# - RunAtLoad=true：launchctl load 时立刻拉起
# - 工作目录 = backend（uvicorn import），ai-service/ai-worker 工作目录 = backend/ai-service
# - 所有 plist 已注入关键 env（PYTHONPATH + 3 个 SERVICE_URL + STASHBOX_ALLOW_DEV_JWT + PATH）
#
# 何时用：
#   • 想让服务开机/登出后自启（持久化）
#   • 让 launchd 守护进程，崩了自动拉起
#
# 何时不用：
#   • 临时调试 / 看 log 不想被 launchd 干扰 → 用 run_dev.sh 或 bin/restart_services.sh
#   • 当前 sandbox 调试场景（plist load 后进程由 launchd 拥有，sandbox 里工具看不到 env）
#
# 注意事项：
#   - install 之前必须先 stop 当前所有 4 服务 + worker（避免端口冲突）
#   - 用了 launchd 后，bin/restart_services.sh 仍能用（它会 kill 旧进程，launchd KeepAlive 会自动再拉起）
set -uo pipefail

AGENTS=(
  "com.stashbox.api-gateway"
  "com.stashbox.user-service"
  "com.stashbox.content-service"
  "com.stashbox.ai-service"
  "com.stashbox.ai-worker"
)

ACTION="${1:-status}"

install() {
  echo "▶ 安装 launchd agents..."
  # 先停当前 4 服务 + worker，避免与 launchd KeepAlive 撞车
  for tag in api-gateway user-service content-service ai-service ai-worker; do
    local pid
    pid=$(lsof -nP -iTCP:"$(port_for "$tag")" -sTCP:LISTEN -t 2>/dev/null) || true
    if [[ -n "$pid" ]]; then
      echo "  停旧 $tag pid=$pid"
      kill -9 $pid 2>/dev/null || true
    fi
  done
  # worker（无端口，用 pgrep）
  pkill -f "arq worker.WorkerSettings" 2>/dev/null || true

  for a in "${AGENTS[@]}"; do
    local plist="$HOME/Library/LaunchAgents/$a.plist"
    if [[ ! -f "$plist" ]]; then
      echo "  ✗ 缺 plist: $plist（先 bin/install_launchd_agents.sh 把 plist 部署到位）"
      continue
    fi
    launchctl load "$plist" 2>&1 | head -3
    echo "  ✓ load $a"
  done

  sleep 5
  echo ""
  echo "=== 健康检查 ==="
  for a in "${AGENTS[@]}"; do
    if launchctl list "$a" >/dev/null 2>&1; then
      echo "  ✓ $a running (pid=$(launchctl list "$a" | awk '{print $1}'))"
    else
      echo "  ✗ $a NOT running"
    fi
  done
}

uninstall() {
  echo "▶ 卸载 launchd agents..."
  for a in "${AGENTS[@]}"; do
    launchctl unload "$HOME/Library/LaunchAgents/$a.plist" 2>/dev/null
    echo "  ✓ unload $a"
  done
  echo ""
  echo "plist 文件仍保留在 ~/Library/LaunchAgents/，下次 install 复用。"
  echo "如要清：  rm -f ~/Library/LaunchAgents/com.stashbox.*.plist"
}

status() {
  echo "=== launchd 状态 ==="
  for a in "${AGENTS[@]}"; do
    if launchctl list "$a" >/dev/null 2>&1; then
      local info
      info=$(launchctl list "$a")
      local pid status
      pid=$(echo "$info" | awk '{print $1}')
      status=$(echo "$info" | awk '{print $2}')
      echo "  ✓ $a  pid=$pid status=$status"
    else
      echo "  ✗ $a  not loaded"
    fi
  done
}

port_for() {
  case "$1" in
    api-gateway) echo 8100 ;;
    user-service) echo 8101 ;;
    content-service) echo 8102 ;;
    ai-service) echo 8103 ;;
    ai-worker) echo 0 ;;  # 无端口
  esac
}

case "$ACTION" in
  install) install ;;
  uninstall) uninstall ;;
  status|"") status ;;
  *) echo "用法: $0 {install|uninstall|status}" >&2; exit 1 ;;
esac