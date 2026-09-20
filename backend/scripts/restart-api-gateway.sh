#!/usr/bin/env bash
# 重启 api-gateway 专用脚本（CP10.6 教训：手动 nohup 启动会漏 env，导致下游 URL 走默认 docker 服务名 → 502）。
#
# 4 个端口:
#   api-gateway  :8100   ←  本脚本目标
#   user-service :8101
#   content-service :8102
#   ai-service :8103
#
# 用法:
#   bash scripts/restart-api-gateway.sh
#
# 验证启动后 env 生效:
#   ps eww $(pgrep -f 'uvicorn.*api-gateway') | grep SERVICE_URL
#   期望看到:USER_SERVICE_URL / CONTENT_SERVICE_URL / AI_SERVICE_URL 都是 http://localhost:810x
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  建议运行: cd backend && ./run_dev.sh" >&2
  exit 1
fi

# 杀掉现存 api-gateway 进程（端口 8100）
EXISTING_PID="$(lsof -ti :8100 2>/dev/null || true)"
if [[ -n "$EXISTING_PID" ]]; then
  echo "▶ 杀掉现存 api-gateway (pid $EXISTING_PID)"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
fi

# 显式 export 下游 URL（关键：pydantic-settings 在模块 import 时锁住）
export USER_SERVICE_URL="http://localhost:8101"
export CONTENT_SERVICE_URL="http://localhost:8102"
export AI_SERVICE_URL="http://localhost:8103"
export PYTHONPATH="$(dirname "$REPO_ROOT"):${PYTHONPATH:-}"

# 启动（cwd 必须是 backend 根，不是 api-gateway 子目录——sys.path[0]=cwd 才能 import api-gateway）
cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m uvicorn api-gateway.main:app --host 0.0.0.0 --port 8100 --log-level info
