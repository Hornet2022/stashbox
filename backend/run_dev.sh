#!/usr/bin/env bash
# 一键启动 4 个 FastAPI 服务（CP1.4：直接 uvicorn，不部署 Docker / K8s）。
# 容器化方案留到 CP1.x 末段单独任务包。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # backend/
REPO_ROOT="$(dirname "$SCRIPT_DIR")"                         # stashbox/（仓库根）
REPO_PARENT="$(dirname "$REPO_ROOT")"                        # 仓库根的父目录（stashbox 包所在目录）
export PYTHONPATH="${REPO_PARENT}:${PYTHONPATH:-}"

# 本地下游地址覆盖（默认是 docker 服务名，本地改 localhost）
export USER_SERVICE_URL="${USER_SERVICE_URL:-http://localhost:8001}"
export CONTENT_SERVICE_URL="${CONTENT_SERVICE_URL:-http://localhost:8002}"
export AI_SERVICE_URL="${AI_SERVICE_URL:-http://localhost:8003}"

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

start_service api-gateway 8000
start_service user-service 8001
start_service content-service 8002
start_service ai-service 8003

cleanup() {
  echo ""
  echo "■ 停止 4 个服务…"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "✅ 4 个服务已启动（Ctrl+C 退出）"
wait
