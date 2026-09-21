#!/usr/bin/env bash
# 重启 content-service 专用脚本。
#
# 端口: content-service :8102   ←  本脚本目标
#
# 用法:
#   bash scripts/restart-content-service.sh
#
# content-service 写音频到 OSS（不在本地），不依赖下游 URL。
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  建议运行: cd backend && ./run_dev.sh" >&2
  exit 1
fi

EXISTING_PID="$(lsof -ti :8102 2>/dev/null || true)"
if [[ -n "$EXISTING_PID" ]]; then
  echo "▶ 杀掉现存 content-service (pid $EXISTING_PID)"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
fi

export PYTHONPATH="$(dirname "$REPO_ROOT"):${PYTHONPATH:-}"
# dev 环境放行默认 JWT secret；生产必须用真 JWT_SECRET 注入（见 config.py 校验）
export STASHBOX_ALLOW_DEV_JWT="${STASHBOX_ALLOW_DEV_JWT:-1}"

cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m uvicorn content-service.main:app --host 0.0.0.0 --port 8102 --log-level info
