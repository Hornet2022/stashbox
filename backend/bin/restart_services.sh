#!/usr/bin/env bash
# restart_services.sh — 听匣后端 4 服务 + arq worker 一键重启（macmini 本地）。
#
# 用法：
#   bin/restart_services.sh              # 全部重启（gateway + user + content + ai + worker）
#   bin/restart_services.sh gateway      # 仅重启 gateway（其它不动）
#   bin/restart_services.sh content      # 仅重启 content-service
#
# 设计要点：
# - 自动 source backend/.env（优先级：env > .env > 脚本兜底）
# - 用 Python subprocess.Popen + start_new_session=True 拉起子进程，
#   脱离父 shell（sandbox 里 nohup ... & 会被 SIGHUP 杀，这个姿势才稳）
# - 端口已 LISTEN 提示先 kill 旧 PID（避免 port already in use）
# - 健康检查：5s 后 curl /health + pgrep worker，给出 1 行汇总
#
# 何时用：
#   • 改了 backend 代码，要重启某服务
#   • 误操作导致端口被占
#   • macmini 重启后服务全没了
#   • 改 backend/.env 后要重新加载
#
# 何时不用：
#   • 首次完全启动走 run_dev.sh（带 trap cleanup 优雅退出）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$BACKEND_DIR")"
REPO_PARENT="$(dirname "$REPO_ROOT")"
LOG_DIR="${LOG_DIR:-/tmp}"
PYTHON_BIN="${PYTHON_BIN:-$BACKEND_DIR/.venv/bin/python}"

# -- 加载 backend/.env（auto-load USER/CONTENT/AI_SERVICE_URL 等覆盖）--
# 与 run_dev.sh 同姿势：set -a 自动 export 所有赋值；后置 set +a。
# 优先级：命令行 export > .env > 脚本 :-兜底。
if [[ -f "$BACKEND_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BACKEND_DIR/.env"
  set +a
  echo "▶ 已加载 backend/.env（$(wc -l < "$BACKEND_DIR/.env" | tr -d ' ') 行）"
fi

# 默认跑全部 4 服务 + worker；命令行第一个参数可指定子集（g/c/u/a/w）
TARGETS="${1:-all}"

# -- 加载 .env（含 USER_SERVICE_URL 等覆盖，避免 gateway fallback 走到 docker 名）--
if [[ -f "$BACKEND_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BACKEND_DIR/.env"
  set +a
fi

# shell 环境兜底（脚本兜底而非 .env 优先）
export USER_SERVICE_URL="${USER_SERVICE_URL:-http://localhost:8101}"
export CONTENT_SERVICE_URL="${CONTENT_SERVICE_URL:-http://localhost:8102}"
export AI_SERVICE_URL="${AI_SERVICE_URL:-http://localhost:8103}"
export STASHBOX_ALLOW_DEV_JWT="${STASHBOX_ALLOW_DEV_JWT:-1}"
export PYTHONPATH="${REPO_PARENT}:${PYTHONPATH:-}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "✗ Python 不可用：$PYTHON_BIN" >&2
  echo "  请先：python -m venv backend/.venv && pip install -r backend/<service>/requirements.txt" >&2
  exit 1
fi

# 服务列表定义
declare -a ALL_SERVICES=(
  "api-gateway:8100:api-gateway.main:app"
  "user-service:8101:user-service.main:app"
  "content-service:8102:content-service.main:app"
  "ai-service:8103:main:app"
)

want_service() {
  # 是否对全:包该服务（按短名）
  # 调用: want_service api-gateway / user-service / ai-service / content-service / worker
  # TARGETS 是 all 或单一名字
  local name="$1" t="$TARGETS"
  [[ "$t" == "all" ]] && return 0
  [[ "$t" == "$name" ]] && return 0
  # 别名：gateway = api-gateway, ai = ai-service, content = content-service, user = user-service, w = worker
  case "$t" in
    gateway|g) [[ "$name" == "api-gateway" ]] && return 0 ;;
    content|c) [[ "$name" == "content-service" ]] && return 0 ;;
    user|u)    [[ "$name" == "user-service" ]] && return 0 ;;
    ai|a)      [[ "$name" == "ai-service" ]] && return 0 ;;
    worker|w)  [[ "$name" == "worker" ]] && return 0 ;;
  esac
  return 1
}

# 把网关进程也单独管理（需要在 Python Popen 子进程里）
stop_by_port() {
  local port="$1"
  # macOS 上 lsof 能取 LISTEN 的 PID；ps axww 在沙箱里会被拒
  local pids
  pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null) || pids=""
  if [[ -n "$pids" ]]; then
    echo "  停旧进程 port=$port pid=$pids"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
}

start_service() {
  local name="$1" port="$2" app="$3"
  if ! want_service "${name%-service}" && ! want_service "$name"; then
    return 0
  fi
  stop_by_port "$port"

  # start_new_session=True 必需：sandbox 内 nohup ... & 会被 SIGHUP 杀
  # 关键：显式把 5 个 env set 进 env dict（父 shell 的 os.environ 可能没有——之前踩过这个坑）
  USER_SERVICE_URL_VAL="${USER_SERVICE_URL:-http://localhost:8101}"
  CONTENT_SERVICE_URL_VAL="${CONTENT_SERVICE_URL:-http://localhost:8102}"
  AI_SERVICE_URL_VAL="${AI_SERVICE_URL:-http://localhost:8103}"
  STASHBOX_ALLOW_DEV_JWT_VAL="${STASHBOX_ALLOW_DEV_JWT:-1}"
  PYTHONPATH_VAL="${PYTHONPATH:-/Users/hornet/work}"
  "$PYTHON_BIN" - "$name" "$port" "$app" "$REPO_ROOT/backend" "$LOG_DIR" \
    "$PYTHONPATH_VAL" \
    "$USER_SERVICE_URL_VAL" \
    "$CONTENT_SERVICE_URL_VAL" \
    "$AI_SERVICE_URL_VAL" \
    "$STASHBOX_ALLOW_DEV_JWT_VAL" <<'PY'
import os, subprocess, sys
name, port, app, cwd, log_dir, pythonpath, user_url, content_url, ai_url, dev_jwt = sys.argv[1:]
# 子进程 env 显式构造：保留父 shell PATH，但**强制** set 5 个关键变量
env = {
    **os.environ,
    "PYTHONPATH": pythonpath,
    "USER_SERVICE_URL": user_url,
    "CONTENT_SERVICE_URL": content_url,
    "AI_SERVICE_URL": ai_url,
    "STASHBOX_ALLOW_DEV_JWT": dev_jwt,
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", ""),
}
log = open(os.path.join(log_dir, f"{name}.log"), "w")
p = subprocess.Popen(
    [os.environ.get("PYTHON_BIN", "/Users/hornet/work/stashbox/backend/.venv/bin/python"),
     "-u", "-m", "uvicorn", app, "--host", "0.0.0.0", "--port", str(port), "--log-level", "info"],
    cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f"  ▶ {name} :{port} pid={p.pid} (CONTENT_SERVICE_URL={content_url})")
PY
}

start_worker() {
  if ! want_service worker; then
    return 0
  fi
  # arq worker 的特点是 ai-service 目录里 ai_worker.WorkerSettings；
  # 用 Popen 跑 cd ai-service && python -m arq worker.WorkerSettings
  local log="$LOG_DIR/ai-worker.log"
  local PYTHONPATH_VAL="${PYTHONPATH:-/Users/hornet/work}"
  "$PYTHON_BIN" - "$REPO_ROOT/backend/ai-service" "$log" "$PYTHON_BIN" "$PYTHONPATH_VAL" <<'PY'
import os, subprocess, sys
cwd, log_path, py, pythonpath = sys.argv[1:]
log = open(log_path, "w")
env = {
    **os.environ,
    "PYTHONPATH": pythonpath,
    "STASHBOX_ALLOW_DEV_JWT": os.environ.get("STASHBOX_ALLOW_DEV_JWT", "1"),
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", ""),
}
p = subprocess.Popen(
    [py, "-u", "-m", "arq", "worker.WorkerSettings"],
    cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f"  ▶ ai-worker pid={p.pid}")
PY
}

echo "▶ 重启目标：$TARGETS"
echo "▶ 使用 Python: $PYTHON_BIN"

# 启动顺序：先下游服务，最后 gateway（避免 gateway 短时间内连不上游）
if want_service content-service; then
  start_service "content-service" 8102 "content-service.main:app"
fi
if want_service user-service; then
  start_service "user-service" 8101 "user-service.main:app"
fi
if want_service ai-service; then
  # ai-service 是 ai-service 目录里的 main:app，cwd 必须是 backend/ai-service
  # （参考 run_dev.sh 第 47 行：cd "$REPO_ROOT/backend/$name" && exec ...）
  stop_by_port 8103
  AI_WORKER_LOG="${AI_WORKER_LOG:-/tmp/ai-worker.log}"
  "$PYTHON_BIN" - "$REPO_ROOT/backend/ai-service" "$AI_WORKER_LOG" "$PYTHON_BIN" "$PYTHONPATH_VAL" \
    "$USER_SERVICE_URL_VAL" "$CONTENT_SERVICE_URL_VAL" "$AI_SERVICE_URL_VAL" \
    "$STASHBOX_ALLOW_DEV_JWT_VAL" <<'PY'
import os, subprocess, sys
ai_cwd, log_path, py, pythonpath, user_url, content_url, ai_url, dev_jwt = sys.argv[1:]
log = open(log_path, "w")
env = {
    **os.environ,
    "PYTHONPATH": pythonpath,
    "USER_SERVICE_URL": user_url,
    "CONTENT_SERVICE_URL": content_url,
    "AI_SERVICE_URL": ai_url,
    "STASHBOX_ALLOW_DEV_JWT": dev_jwt,
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", ""),
}
p = subprocess.Popen(
    [py, "-u", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8103", "--log-level", "info"],
    cwd=ai_cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f"  ▶ ai-service :8103 pid={p.pid}")
PY
  sleep 2
  start_worker
fi
if want_service api-gateway; then
  start_service "api-gateway" 8100 "api-gateway.main:app"
fi

# 健康检查
sleep 5
echo ""
echo "=== 健康检查 ==="
for entry in "${ALL_SERVICES[@]}"; do
  IFS=':' read -r name port app <<< "$entry"
  if [[ "$port" == "0" ]]; then continue; fi
  # 端口是否 LISTEN
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    # /health 检查（仅 gateway 暴露 /health；其它服务没有就只看端口）
    code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" 2>/dev/null || echo '?')"
    if [[ "$code" == "200" || "$code" == "?" ]]; then
      echo "  ✓ :$port $name  up (health=$code)"
    else
      echo "  ✗ :$port $name  health=$code（端口在但 /health 返 $code，看 $LOG_DIR/$name.log）"
    fi
  else
    echo "  ✗ :$port $name  未 LISTEN"
  fi
done

# worker
if pgrep -fl "arq worker.WorkerSettings" >/dev/null 2>&1; then
  echo "  ✓ ai-worker  alive"
else
  echo "  ✗ ai-worker  未运行（看 $LOG_DIR/ai-worker.log）"
fi

echo ""
echo "✅ 完成。日志：$LOG_DIR/{api-gateway,user-service,content-service,ai-service,ai-worker}.log"
echo "   停全部：kill \$(lsof -nP -iTCP:8100-8103 -t)  ;  stop one: bin/restart_services.sh gateway"