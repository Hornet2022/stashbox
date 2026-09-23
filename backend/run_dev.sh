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

# homebrew 工具（ffmpeg 等）加入 PATH：蒸馏 step4 拼音频时裸调 `ffmpeg` 能找到
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# 本地下游地址覆盖（默认是 docker 服务名，本地改 localhost:810x）
# 如果 backend/.env 里有同名变量则优先使用；env 不存在则走 :-兜底。
# 这样：
#   - macmini 本地默认 .env 里有 USER_SERVICE_URL=http://localhost:8101 → 自动走本地
#   - 用户从 .env.example 拷出来但还没改 → 仍走 docker 名（避免把错误配置静默吞掉）
#   - CI / 容器化场景：直接 export USER_SERVICE_URL=... 即可覆盖（优先级最高）
export USER_SERVICE_URL="${USER_SERVICE_URL:-http://localhost:8101}"
export CONTENT_SERVICE_URL="${CONTENT_SERVICE_URL:-http://localhost:8102}"
export AI_SERVICE_URL="${AI_SERVICE_URL:-http://localhost:8103}"
export STASHBOX_ALLOW_DEV_JWT="${STASHBOX_ALLOW_DEV_JWT:-1}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# 自动加载 backend/.env（如果存在），覆盖同名变量后再 export。
# 用 set -a 自动 export 所有赋值；set +a 关闭。
# 优先级：命令行 export > .env 文件 > 脚本 :-兜底。
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
  echo "▶ 已加载 backend/.env（$(wc -l < "$SCRIPT_DIR/.env" | tr -d ' ') 行）"
fi

# Python 解释器解析（按优先级回退，便于不同机器复用，不写死绝对路径）：
#   1) $PYTHON_BIN 环境变量（可显式指向任意已装依赖的 venv）
#   2) 工程自带 backend/.venv/bin/python
#   3) 系统 python3（需已装 fastapi/uvicorn/arq）
if [[ -n "${PYTHON_BIN:-}" && -x "$PYTHON_BIN" ]]; then
  :
elif [[ -x "$REPO_ROOT/backend/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/backend/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import uvicorn, arq" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "✗ 找不到可用的 Python 解释器（需已安装 fastapi/uvicorn/arq 等依赖）" >&2
  echo "  方式一：创建工程 venv  →  python -m venv backend/.venv && pip install -r backend/<service>/requirements.txt" >&2
  echo "  方式二：指定已有 venv  →  PYTHON_BIN=/path/to/venv/bin/python ./run_dev.sh" >&2
  exit 1
fi
echo "▶ 使用 Python: $PYTHON_BIN"

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

# CP3.5-pre-3：ai-service 的蒸馏任务走 Arq 队列，需要独立 worker 进程消费。
#   ai-service 目录名带连字符（不是合法包名），worker 是顶层模块 —— 必须 cd 进去
#   用 `python -m arq`（-m 会把 cwd 加进 sys.path），否则 import 不到 worker。
#   日志单独落文件（默认 /tmp/worker.log），跟 4 个 uvicorn 的 stdout 分开好看。
AI_WORKER_LOG="${AI_WORKER_LOG:-/tmp/worker.log}"
(
  cd "$REPO_ROOT/backend/ai-service" \
    && exec "$PYTHON_BIN" -m arq worker.WorkerSettings
) >> "$AI_WORKER_LOG" 2>&1 &
PIDS+=("$!")
echo "▶ ai-worker 启动 (arq, pid $!, log $AI_WORKER_LOG)"

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
