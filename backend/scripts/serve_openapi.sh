#!/usr/bin/env bash
# CP6.6：本地预览 OpenAPI 文档站（Redoc 静态页）。
#
# 只起一个静态文件服务，把 backend/docs/openapi/ 目录挂到 http://localhost:8888/。
# 红线：只本地预览，不部署公网、不接反向代理。
#
# 用法：
#   bash backend/scripts/serve_openapi.sh          # 默认 8888
#   PORT=9000 bash backend/scripts/serve_openapi.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCS_DIR="$REPO_ROOT/backend/docs/openapi"
PORT="${PORT:-8888}"

if [[ ! -f "$DOCS_DIR/stashbox-openapi.json" ]]; then
  echo "✗ 缺少 $DOCS_DIR/stashbox-openapi.json" >&2
  echo "  请先执行：python backend/scripts/export_openapi.py" >&2
  exit 1
fi

# 优先用仓库 venv 的 python（保证能 import，虽然 http.server 是 stdlib）
PYTHON_BIN="$REPO_ROOT/backend/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

cd "$DOCS_DIR"
echo "▶ OpenAPI 文档站已启动"
echo "  页面：  http://localhost:${PORT}/"
echo "  schema：http://localhost:${PORT}/stashbox-openapi.json"
echo "  （Ctrl+C 退出）"
exec "$PYTHON_BIN" -m http.server "$PORT"
