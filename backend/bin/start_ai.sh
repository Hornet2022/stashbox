#!/usr/bin/env bash
# start_ai.sh — 用 nohup + disown 拉起 ai-service :8103 和 ai-worker (Arq)。
# 不依赖 launchd（sandbox 内 launchctl load 没权限）。
# 用法：./bin/start_ai.sh start|stop|restart|status

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
AI_DIR="$BACKEND_DIR/ai-service"
LOG_AI="/tmp/stashbox-ai-service.out"
LOG_W="/tmp/stashbox-ai-worker.out"
PID_AI="/tmp/stashbox-ai-service.pid"
PID_W="/tmp/stashbox-ai-worker.pid"

start_one() {
    local cwd="$1" label="$2" log="$4" cmd="$5" pidfile="$6"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "  $label already running pid=$(cat "$pidfile")"
        return 0
    fi
    # 在 sandbox 内 nohup + & 都会被父 shell 退出时的 SIGHUP 带走。
    # 用 python subprocess.Popen(start_new_session=True) 完全脱离父进程组
    # （等同 setsid，且在 sandbox 内行为更可预期）。
    "$PYTHON_BIN" -c "
import os, subprocess, sys
cwd, log, py, cmd = sys.argv[1:5]
cmd_args = cmd.split() if isinstance(cmd, str) else cmd
log_f = open(log, 'w')
env = {**os.environ, 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:' + os.environ.get('PATH', '')}
p = subprocess.Popen(
    [py] + cmd.split()[1:] if cmd.startswith('-u') else cmd.split(),
    cwd=cwd, env=env,
    stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(p.pid)
" "$cwd" "$log" "$PYTHON_BIN" "$cmd" > /tmp/start_ai.tmp
    local pid=$(cat /tmp/start_ai.tmp)
    rm -f /tmp/start_ai.tmp
    echo "$pid" >"$pidfile"
    echo "  $label pid=$pid log=$log"
}

start_all() {
    echo "▶ 拉起 ai-service :8103"
    start_one "$AI_DIR" "ai-service" "" "$LOG_AI" "-u -m uvicorn main:app --host 0.0.0.0 --port 8103 --log-level info" "$PID_AI"
    sleep 2
    echo "▶ 拉起 ai-worker (Arq)"
    start_one "$AI_DIR" "ai-worker" "" "$LOG_W" "-u -m arq worker.WorkerSettings" "$PID_W"
}

stop_all() {
    for pf in "$PID_AI" "$PID_W"; do
        if [[ -f "$pf" ]]; then
            local pid
            pid=$(cat "$pf")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                echo "  stopped $pf pid=$pid"
            fi
            rm -f "$pf"
        fi
    done
}

status() {
    for pf in "$PID_AI" "$PID_W"; do
        local label
        label=$(basename "$pf" .pid)
        if [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null; then
            echo "  OK $label pid=$(cat "$pf")"
        else
            echo "  DEAD $label"
        fi
    done
}

ACTION="${1:-start}"

if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$BACKEND_DIR/.env"
    set +a
fi
export PYTHONPATH="/Users/hornet/work:${PYTHONPATH:-}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

case "$ACTION" in
    start)   start_all ;;
    stop)    stop_all ;;
    restart) stop_all; sleep 2; start_all ;;
    status)  status ;;
    *)       echo "usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac