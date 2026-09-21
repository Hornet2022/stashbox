#!/usr/bin/env bash
# 重启 ai-service 专用脚本。
#
# 端口: ai-service :8103   ←  本脚本目标
#
# 用法:
#   bash scripts/restart-ai-service.sh
#
# ai-service 调 LLM（mock 在本地），需要 REDIS_URL（任务队列）。
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  建议运行: cd backend && ./run_dev.sh" >&2
  exit 1
fi

EXISTING_PID="$(lsof -ti :8103 2>/dev/null || true)"
if [[ -n "$EXISTING_PID" ]]; then
  echo "▶ 杀掉现存 ai-service (pid $EXISTING_PID)"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
fi

export PYTHONPATH="$(dirname "$REPO_ROOT"):${PYTHONPATH:-}"

cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m uvicorn ai-service.main:app --host 0.0.0.0 --port 8103 --log-level info
