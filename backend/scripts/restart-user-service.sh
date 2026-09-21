#!/usr/bin/env bash
# 重启 user-service 专用脚本。
#
# 端口: user-service :8101   ←  本脚本目标
#
# 用法:
#   bash scripts/restart-user-service.sh
#
# 与 restart-api-gateway.sh 同款：杀进程 → 显式 export 下游 URL → uvicorn 启动。
# user-service 是叶子服务（不调下游），但仍需 DATABASE_URL / REDIS_URL / JWT_SECRET。
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  建议运行: cd backend && ./run_dev.sh" >&2
  exit 1
fi

EXISTING_PID="$(lsof -ti :8101 2>/dev/null || true)"
if [[ -n "$EXISTING_PID" ]]; then
  echo "▶ 杀掉现存 user-service (pid $EXISTING_PID)"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
fi

export PYTHONPATH="$(dirname "$REPO_ROOT"):${PYTHONPATH:-}"

cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m uvicorn user-service.main:app --host 0.0.0.0 --port 8101 --log-level info
