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

# 共享的启停逻辑（2026-10-21 抽出：只杀 LISTEN 的进程 + launchd 托管时用 kickstart）
# shellcheck source=scripts/_service_ctl.sh
source "$(dirname "${BASH_SOURCE[0]}")/_service_ctl.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ venv 不存在：$PYTHON_BIN" >&2
  echo "  建议运行: cd backend && ./run_dev.sh" >&2
  exit 1
fi

export PYTHONPATH="$(dirname "$(dirname "$REPO_ROOT")"):${PYTHONPATH:-}"  # stashbox 包根在仓库父目录（run_dev.sh 同款）
# dev 环境放行默认 JWT secret；生产必须用真 JWT_SECRET 注入（见 config.py 校验）
export STASHBOX_ALLOW_DEV_JWT="${STASHBOX_ALLOW_DEV_JWT:-1}"

cd "$REPO_ROOT"
restart_service "com.stashbox.user-service" 8101 "user-service" \
  "$PYTHON_BIN" -m uvicorn user-service.main:app --host 0.0.0.0 --port 8101 --log-level info
