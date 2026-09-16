#!/usr/bin/env bash
# 一键启动 4 个 FastAPI 服务（直接 uvicorn，不部署 Docker / K8s）。
# 容器化方案留到 CP1.x 末段单独任务包。
#
# 端口说明（CP1.5 起）：默认 8100-8103。
#   原因：本机 8000/8001/8002 已被其它项目进程占用（CP1.4 review 发现），
#   故整体平移到 8100 段，避免踩坑。
#     api-gateway  :8100  user-service :8101  content-service :8102  ai-service :8103
#
# 导入说明：stashbox 包通过 PYTHONPATH（仓库根父目录）导入，本脚本已自动设置。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # backend/
REPO_ROOT="$(dirname "$SCRIPT_DIR")"                         # stashbox/（仓库根）
REPO_PARENT="$(dirname "$REPO_ROOT")"                        # 仓库根的父目录（stashbox 包所在目录）
export PYTHONPATH="${REPO_PARENT}:${PYTHONPATH:-}"

# 本地下游地址覆盖（默认是 docker 服务名，本地改 localhost:810x）
export USER_SERVICE_URL="${USER_SERVICE_URL:-http://localhost:8101}"
export CONTENT_SERVICE_URL="${CONTENT_SERVICE_URL:-http://localhost:8102}"
export AI_SERVICE_URL="${AI_SERVICE_URL:-http://localhost:8103}"

PYTHON_BIN="$REPO_ROOT/backend/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  请先创建：python -m venv backend/.venv && pip install -r backend/<service>/requirements.txt" >&2
  exit 1
fi

PIDS=()
start_service() {
  local name="$1" port="$2"
  ( cd "$REPO_ROOT/backend/$name" && exec "$PYTHON_BIN" -m uvicorn main:app --host 0.0.0.0 --port "$port" --log-level info ) &
  PIDS+=("$!")
  echo "▶ $name 启动 (port $port, pid $!)"
}

start_service api-gateway 8100
start_service user-service 8101
start_service content-service 8102
start_service ai-service 8103

cleanup() {
  echo ""
  echo "■ 停止 4 个服务…"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# CP1.6：配额月度重置定时器由 user-service 在 startup 时拉起
#   （quota_service.quota_reset_loop，每小时检查一次，跨月则 quota_used=0；CP7 换 apscheduler）
#   手动触发：POST http://localhost:8101/api/v1/users/me/quota/reset-monthly（需 JWT）

echo "✅ 4 个服务已启动（Ctrl+C 退出）"
wait
