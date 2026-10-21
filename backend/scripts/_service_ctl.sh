#!/usr/bin/env bash
# 服务启停的共享逻辑（2026-10-21 抽出）。
#
# 为什么要抽出来：4 个 scripts/restart-*.sh 原本各自复制了一段**有 bug** 的 kill 逻辑，
# 而 bin/restart_services.sh 里写的是**正确**的那版 —— 两套写法并存，
# 于是"用单服务脚本重启"这条路一直在静默失败。
#
# 原来那段代码的两个错
# ------------------------
# 1) `lsof -ti :8101` 匹配的是**端口上的任何 socket**，不只 LISTEN。
#    网关和 user-service 之间有 keep-alive 连接，于是这个命令一次返回两个 pid：
#
#        72485   ← 网关（只是跟 8101 有条连接）
#        93547   ← user-service 真正的监听者
#
#    然后 `kill "$EXISTING_PID"` 收到的是**一个多行字符串**，bash 把它当成
#    一个非法 pid → kill 报错 → `2>/dev/null || true` 把错误吞掉。
#    结果：老进程活着，新进程也起来了，两边都"成功"了。
#
# 2) 更阴的是：老进程是 launchd 管的（KeepAlive=true，--host 127.0.0.1），
#    脚本自己起的那个是 --host 0.0.0.0。macOS 上 127.0.0.1 的 bind 更具体，
#    **连 localhost 的请求命中的是老进程**。
#
#    于是本地表现为：改完代码 → 跑重启脚本 → 看着起来了 → 请求行为却完全没变。
#    这是最难查的一类假成功（和"改了代码没生效"完全同一种症状）。
#
# 现在的口径
# ----------
# - 端口上**只**杀 LISTEN 的进程（`-sTCP:LISTEN`），且不吞错误
# - 杀完**确认端口真的空了**，还有别人占着就明确报错退出，不假装成功
# - 如果 launchd 托管着这个服务 → 用 `launchctl kickstart -k` 重启，
#   脚本**不再自己起一个**（两个监督者起同一端口就是上面那个坑的根源）

# ── 端口上真正在监听的 pid（可能多个）
port_listener_pids() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true
}

# ── launchd 是否托管着这个 label
launchd_owns() {
  local label="$1"
  launchctl list "$label" >/dev/null 2>&1
}

# ── 杀掉端口上的监听进程，并等端口真正空出来
#
# 返回 0 = 端口已空；返回 1 = 还有别人占着（调用方应当报错退出）
stop_listeners() {
  local port="$1"
  local name="$2"
  local pids
  pids="$(port_listener_pids "$port")"

  if [[ -z "${pids//[[:space:]]/}" ]]; then
    return 0
  fi

  echo "▶ 停掉占住 :$port 的进程：$(echo "$pids" | tr '\n' ' ')"
  # 不用 `kill "$pids"`：那是把多行当一个 pid。这里靠 word splitting 分开杀。
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true

  # 等 5 秒让端口释放（SIGTERM → 优雅退出）
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    if [[ -z "$(port_listener_pids "$port")" ]]; then
      return 0
    fi
  done

  # 还没走 → 强杀一次
  pids="$(port_listener_pids "$port")"
  if [[ -n "${pids//[[:space:]]/}" ]]; then
    echo "▶ SIGTERM 没停掉，强杀：$(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi

  if [[ -n "$(port_listener_pids "$port")" ]]; then
    echo "✗ :$port 仍被占用（$name 没能独占这个端口）。" >&2
    echo "  占用者：$(port_listener_pids "$port" | tr '\n' ' ')" >&2
    echo "  可能是别的进程占着，或 launchd 立刻把它拉起来了。" >&2
    echo "  查：lsof -nP -iTCP:$port -sTCP:LISTEN" >&2
    return 1
  fi
  return 0
}

# ── 统一的"重启一个服务"入口
#
# 用法：restart_service "<label>" <port> <名字> <启动命令...>
# - launchd 托管 → kickstart，然后直接返回（不再自己起）
# - 否则 → 杀掉监听者 → 执行启动命令
restart_service() {
  local label="$1"
  local port="$2"
  local name="$3"
  shift 3

  if launchd_owns "$label"; then
    echo "▶ $name 由 launchd 托管（$label），用 launchctl kickstart 重启"
    echo "  （不再手动起进程：两个监督者抢同一个端口 = 老进程继续对外服务的那个坑）"
    echo "  注意：走 launchd 时环境变量来自 plist + backend/.env，脚本里的 export 不生效"
    launchctl kickstart -k "gui/$(id -u)/$label"
    # 等它真的绑上端口 —— 固定 sleep 1s 会竞态：
    # 服务的启动要几百毫秒到几秒不等（要连 PG/Redis、跑 migration 检查），
    # 早判一次就误报"启动失败"，其实它起来得挺慢。
    for _ in $(seq 1 30); do
      [[ -n "$(port_listener_pids "$port")" ]] && break
      sleep 0.5
    done
    if [[ -z "$(port_listener_pids "$port")" ]]; then
      echo "✗ kickstart 之后 :$port 上没有监听者（等了 15s）" >&2
      return 1
    fi
    echo "✓ $name 已重启（launchd），监听 :$port 的 pid：$(port_listener_pids "$port" | tr '\n' ' ')"
    return 0
  fi

  if ! stop_listeners "$port" "$name"; then
    return 1
  fi
  echo "▶ 启动 $name (:$port)"
  # exec：让 uvicorn 接管这个 shell，信号直达进程（和原来的 exec 写法一致）
  exec "$@"
}
