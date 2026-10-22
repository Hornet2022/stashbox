#!/usr/bin/env bash
#
# Stashbox 2 GiB 生产机 —— 主机调优脚本
# ==========================================
#
# 目标机器：阿里云轻量应用服务器，**固定规格**
#     2 vCPU / 2 GiB RAM / 40 GiB 系统盘 / 200 Mbps / Ubuntu 24.04
#
# 做的事（每一步都是幂等的，重复跑安全）：
#   1. 创建服务用户 stashbox（system 用户，无密码，nologin）
#   2. 建运行目录（/opt/stashbox、/etc/stashbox、/var/lib/stashbox、/var/log/stashbox）
#   3. 写 PostgreSQL 调优片段（shared_buffers / effective_cache_size / …）
#   4. 写 Redis 调优片段（maxmemory / maxmemory-policy / requirepass / save）
#   5. 建 swap 文件（2 GiB 机器上最有价值的保险）
#   6. 限容 journald + 装 logrotate
#   7. 装 sysctl（vm.swappiness 等）与 MALLOC_ARENA_MAX
#   8. 给 postgresql / redis / nginx 装 cgroup 内存上限（drop-in）
#   9. 打印"改了什么 + 怎么回滚"
#
# 用法：
#     sudo bash tune-host.sh                     # 只调优，不装软件
#     sudo bash tune-host.sh --install-deps      # 先 apt 装依赖再调优
#     sudo bash tune-host.sh --dry-run           # 只打印要做什么，不动系统
#     sudo bash tune-host.sh --restart-services  # 调优后重启 PG/Redis/nginx
#
# 选项：
#     --env-file PATH     环境变量文件（默认 /etc/stashbox/stashbox.env）
#                        —— requirepass 从这里读 REDIS_PASSWORD
#     --swap-size SIZE    swap 文件大小（默认 2G）
#     --service-user NAME 服务用户名（默认 stashbox）
#     --install-deps      apt 安装 nginx / postgresql / redis-server / ffmpeg / python3-venv
#     --restart-services  调优后 systemctl restart postgresql / redis-server / nginx
#     --dry-run           什么都不改，只打印
#     -h | --help         本帮助
#
# ⚠️ 这个脚本**不会**替你填密钥、不会改你的 env 文件里的值、
#    也不会在默认情况下重启正在跑的服务（改了 PG 的 shared_buffers
#    这类 postmaster 参数必须重启才生效，脚本会提示你手动重启）。

set -euo pipefail

# ── 默认值 ────────────────────────────────────────────────────────────
# SCRIPT_DIR：脚本自身所在目录（读 systemd/ 下的 unit 算内存账本要用）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/etc/stashbox/stashbox.env"
SWAP_SIZE="2G"
SWAP_FILE="/swapfile"
SERVICE_USER="stashbox"
INSTALL_DEPS=0
RESTART_SERVICES=0
DRY_RUN=0
APP_ROOT="/opt/stashbox"
STATE_DIR="/var/lib/stashbox"
LOG_DIR="/var/log/stashbox"
CONF_DIR="/etc/stashbox"

# 物理内存（MB）—— **实测值，不标称值**。
# 轻量 2 GiB 机器的 guest 实测只有 1613MB（云厂商划走了一部分给
# hypervisor / 缓存）。所有内存上限的分配都以这个数为准，
# 而不是「2 GiB = 2048M」那个假设 —— 拿 2048 算会超出物理内存 19%。
PHYS_MEM_MB="$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')"
if [ -z "${PHYS_MEM_MB:-}" ] || [ "$PHYS_MEM_MB" -lt 256 ] 2>/dev/null; then
  PHYS_MEM_MB=2048   # free 不可用时退回标称值
fi

# 回滚清单（最后统一打印）
ROLLBACK=()

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?--env-file 需要参数}"; shift 2 ;;
    --swap-size) SWAP_SIZE="${2:?--swap-size 需要参数}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:?--service-user 需要参数}"; shift 2 ;;
    --install-deps) INSTALL_DEPS=1; shift ;;
    --restart-services) RESTART_SERVICES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) printf '未知参数：%s（用 --help 看用法）\n' "$1" >&2; exit 2 ;;
  esac
done

# ── 小工具 ────────────────────────────────────────────────────────────
say()  { printf '▶ %s\n' "$*"; }
info() { printf '  · %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
skip() { printf '  - %s\n' "$*"; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

# 命令是否存在。
# ⚠️ 这个 helper 之前**根本没定义**，而脚本里有三处在调它
#    （have ufw / have firewall-cmd / have ss）—— 全部报
#    "have: command not found"，于是：
#      · ufw 分支永远走 else → 防火墙检查形同虚设
#      · ss 分支永远不执行 → **8100-8103 是否监听在非回环地址上
#        这项最关键的安全检查从未真正跑过**
#    补上定义（不用 `command -v` 的输出污染 stdout）。
have() { command -v "$1" >/dev/null 2>&1; }

# 需要 root（写 /etc、/sysctl、/swapfile、useradd 都要）
if [ "$(id -u)" -ne 0 ]; then
  die "请用 root 跑：sudo bash $0 $*"
fi

# 真正执行一条命令；--dry-run 时只打印
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

# 写一个文件（带权限）；--dry-run 时只打印内容摘要
write_file() {
  local path="$1" mode="$2" owner="$3"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] 写 %s（mode=%s owner=%s，%s 行）\n' \
      "$path" "$mode" "$owner" "$(printf '%s\n' "$CONTENT" | wc -l | tr -d ' ')"
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$CONTENT" > "$path"
  chmod "$mode" "$path"
  if [ -n "$owner" ]; then
    chown "$owner" "$path"
  fi
  say "写入 $path"
}

# 往文件尾部追加一行（幂等：已存在就不加）
append_line_once() {
  local path="$1" line="$2"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] 追加到 %s：%s\n' "$path" "$line"
    return 0
  fi
  if [ -f "$path" ] && grep -Fqx "$line" "$path"; then
    skip "$path 已含该行（跳过）"
    return 0
  fi
  printf '%s\n' "$line" >> "$path"
  say "追加到 ${path}：$line"
}

# 从 env 文件里读一个变量（用 awk 解析，**不 source** —— env 文件是配置不是脚本）
envfile_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -v wanted="$key" '
    BEGIN { res = "" }
    {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (line == "") next
      if (substr(line, 1, 1) == "#") next
      i = index(line, "=")
      if (i == 0) next
      k = substr(line, 1, i - 1)
      val = substr(line, i + 1)
      gsub(/[ \t]+$/, "", k)
      if (k != wanted) next
      sub(/^[ \t]+/, "", val)
      if (length(val) >= 2) {
        q = substr(val, 1, 1)
        if ((q == "\"" || q == "\047") && substr(val, length(val), 1) == q) {
          val = substr(val, 2, length(val) - 2)
        }
      }
      res = val
    }
    END { if (res != "") printf "%s", res }
  ' "$ENV_FILE" 2>/dev/null
}

restart_svc() {
  local unit="$1"
  if [ "$RESTART_SERVICES" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    say "重启 $unit"
    systemctl restart "$unit" || warn "$unit 重启失败 —— 手工查一下"
  fi
}

# ═════════════════════════════════════════════════════════════════════
# 0. 依赖安装（可选）
# ═════════════════════════════════════════════════════════════════════
if [ "$INSTALL_DEPS" -eq 1 ]; then
  say "安装系统依赖"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] apt-get install -y nginx postgresql redis-server ffmpeg python3-venv python3-pip\n'
  else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # ffmpeg 是音频链路的硬依赖（拼音频 / 多码率转码都真的 fork 它）；
    # nginx 是 TLS 入口；postgresql / redis 是这台机器上的数据面。
    apt-get install -y --no-install-recommends \
      nginx postgresql redis-server ffmpeg python3-venv python3-pip ca-certificates
    ROLLBACK[${#ROLLBACK[@]}]="卸载依赖：apt-get remove -y nginx postgresql redis-server ffmpeg python3-venv python3-pip"
  fi
fi

# ═════════════════════════════════════════════════════════════════════
# 0-b. pip / poetry 源
# ═════════════════════════════════════════════════════════════════════
#
# ⚠️ 为什么必须配：默认 pypi.org 在这台机器上**超时**（实测 10s 读超时，
#    pip 装 edge-tts 时直接 ReadTimeoutError 崩掉）。
#    阿里云内网源 mirrors.cloud.aliyuncs.com 实测 0.086s —— 快 100 倍。
#    同地域的 ECS / 轻量应用服务器走阿里云内网，不产生公网流量费。
#
# ⚠️ 为什么单独一段而不是塞进 --install-deps：
#    装依赖（poetry install）发生在 venv 建好**之后**，那时
#    /etc/pip.conf 必须已经就位，否则又是超时。
say "pip / poetry 源"
PIP_MIRROR="https://mirrors.cloud.aliyuncs.com/pypi/simple/"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '    [dry-run] 写 /etc/pip.conf（index-url = %s）\n' "$PIP_MIRROR"
else
  if [ -f /etc/pip.conf ] && grep -q "mirrors.cloud.aliyuncs.com" /etc/pip.conf; then
    skip "/etc/pip.conf 已指向阿里云内网源"
  else
    cat > /etc/pip.conf <<EOF
[global]
index-url = ${PIP_MIRROR}
trusted-host = mirrors.cloud.aliyuncs.com
timeout = 60
EOF
    say "写入 /etc/pip.conf（${PIP_MIRROR}）"
  fi

  # poetry 2.x 优先用 pyproject 里的 source 配置，/etc/pip.conf 它不看，
  # 所以要单独写一份 config.toml。
  POETRY_CFG_DIR="${APP_ROOT}/.config/pypoetry"
  mkdir -p "$POETRY_CFG_DIR"
  if [ -f "${POETRY_CFG_DIR}/config.toml" ] && grep -q "mirrors.cloud.aliyuncs.com" "${POETRY_CFG_DIR}/config.toml"; then
    skip "poetry 源已指向阿里云内网源"
  else
    cat > "${POETRY_CFG_DIR}/config.toml" <<EOF
[[tool.poetry.source]]
name = "aliyun"
url = "${PIP_MIRROR}"
priority = "default"
EOF
    chown -R "$SERVICE_USER:$SERVICE_USER" "${APP_ROOT}/.config" 2>/dev/null || true
    say "写入 ${POETRY_CFG_DIR}/config.toml"
  fi
fi

# ═════════════════════════════════════════════════════════════════════
# 1. 服务用户
# ═════════════════════════════════════════════════════════════════════
say "服务用户 $SERVICE_USER"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  skip "$SERVICE_USER 已存在（uid=$(id -u "$SERVICE_USER")）"
else
  # --system：UID 在 100 段，不可登录；nologin：不能开 shell。
  # home 指向 /opt/stashbox（代码 + venv 的父目录），配合 ProtectHome=true
  # 服务根本碰不到任何人的家目录。
  run useradd --system --user-group --home-dir "$APP_ROOT" \
              --shell /usr/sbin/nologin "$SERVICE_USER"
  ROLLBACK[${#ROLLBACK[@]}]="删用户：userdel -r $SERVICE_USER"
fi

# ═════════════════════════════════════════════════════════════════════
# 2. 目录
# ═════════════════════════════════════════════════════════════════════
say "运行目录"
for d in "$APP_ROOT" "$CONF_DIR" "$STATE_DIR" "$STATE_DIR/tmp" "$STATE_DIR/data" "$LOG_DIR"; do
  run mkdir -p "$d"
done
# /etc/stashbox 里的 env 文件含密钥：只有 root 和服务用户能读。
run chmod 0750 "$CONF_DIR"
run chown root:"$SERVICE_USER" "$CONF_DIR"
run chown -R "$SERVICE_USER":"$SERVICE_USER" "$STATE_DIR" "$LOG_DIR"
run chmod 0750 "$STATE_DIR" "$STATE_DIR/tmp" "$STATE_DIR/data" "$LOG_DIR"
ROLLBACK[${#ROLLBACK[@]}]="删目录：rm -rf $APP_ROOT $CONF_DIR $STATE_DIR $LOG_DIR"

# ═════════════════════════════════════════════════════════════════════
# 3. PostgreSQL 调优
# ═════════════════════════════════════════════════════════════════════
say "PostgreSQL 调优"
pg_ver="$(ls -1 /etc/postgresql 2>/dev/null | sort -V | tail -1)"
if [ -z "$pg_ver" ] || [ ! -d "/etc/postgresql/$pg_ver/main" ]; then
  skip "没找到 /etc/postgresql/<ver>/main —— 跳过（先装 PostgreSQL 或手动改）"
else
  pg_conf="/etc/postgresql/$pg_ver/main/postgresql.conf"
  pg_snippet="/etc/postgresql/$pg_ver/main/conf.d/99-stashbox-2g.conf"

  # postgresql.conf 要真的 include 这个片段。
  # Debian/Ubuntu 默认 `include_dir = 'conf.d'`（生效状态）；万一被注释掉了，
  # 这里补回来 —— 片段写了但不 include 等于没写。
  if ! grep -qE "^[[:space:]]*include_dir[[:space:]]*=" "$pg_conf" 2>/dev/null; then
    append_line_once "$pg_conf" "include_dir = 'conf.d'"
  fi

  CONTENT="# Stashbox 2 GiB 机器的 PostgreSQL 调优（由 tune-host.sh 生成）
# 目标机器：2 vCPU / 2 GiB RAM / 40 GiB 系统盘，小负载（内测量级）。
# 原则：**把内存花在真正有用的缓存上**，别在连接数和排序缓冲上花光。
#
# shared_buffers = 64MB
#   PostgreSQL 的共享内存池。默认 128MB 在这台机器上偏大：
#   实测 PG 空载 RSS 就有 200-300MB（shared_buffers + 连接 + 进程开销）。
#   ⚠️ 这台机器**物理内存只有 1613MB**（不是标称的 2048MB），
#      所以从 96MB 进一步降到 64MB，给 cgroup 的 320M 上限留出余量。
#      经验值：内存的 25% 左右，这里 64MB / 1613MB ≈ 4% —— 刻意压低，
#      因为 PG 的 RSS 还要叠加连接数 × 每连接开销。
#
# effective_cache_size = 384MB
#   **不是**真实分配，只告诉查询计划器「OS page cache 大概有这么多可用」。
#   给大了会让 PG 选错索引（本该用 index scan 的地方选 seq scan）。
#   按 1613MB 物理内存的 24% 取值。
#
# max_connections = 30
#   每个后端连接大约 5-8MB。30 × 6MB ≈ 180MB。
#   本机 5 个 Python 进程，每个 POSTGRES_POOL_SIZE=4（常驻 20 条），
#   剩下 10 条留给突发（max_overflow）和运维连接 —— 内测量级够用。
#   ⚠️ 调大之前先算：连接数 × 每连接开销，别把内存花光。
#   （原 40 在 2048MB 假设下合理；1613MB 上必须下调，否则 PG 触顶被 OOM kill。）
#
# work_mem = 4MB
#   每个排序/哈希节点的上限，**乘以并发操作数**才是真实占用。
#   4MB × 30 连接 = 最坏 120MB，所以这里给得很小。
#   这个库的查询都是主键/索引点查，用不上大排序。
#
# maintenance_work_mem = 32MB
#   建索引 / VACUUM 用。一次性动作，可以比 work_mem 大一点，
#   但别大到让 PG 在索引构建时把 2 GiB 撑爆。
#
# wal_* —— WAL 是写放大的来源，写多了既占盘又制造 IO 峰值：
#   min_wal_size = 80MB
#       WAL 段的预分配下限。太小会反复创建/回收 16MB 的段（IO 抖动）。
#   max_wal_size = 512MB
#       checkpoint 的上限。太大 = 脏页在内存里堆着（吃 RAM），
#       太小 = checkpoint 频繁（IO 尖峰）。512MB 是 40GB 盘上的稳妥值。
#   checkpoint_completion_target = 0.9
#       把 checkpoint 的写回摊到 90% 的时间窗口里，避免一次性写回造成 IO 尖峰。
#   checkpoint_timeout = 15min
#       兜底：即使没有写入压力也定期做一次 checkpoint。
#   wal_compression = on
#       压缩 WAL，减少实际写盘量（CPU 换 IO，CPU 在这台机器上不是瓶颈）。
#
# 改完要 **restart**（shared_buffers / max_connections 是 postmaster 参数，
# reload 不生效）。tune-host.sh 默认不替你重启 —— 它会打印命令。
shared_buffers = 64MB
effective_cache_size = 384MB
max_connections = 30
work_mem = 4MB
maintenance_work_mem = 32MB
min_wal_size = 80MB
max_wal_size = 512MB
checkpoint_completion_target = 0.9
checkpoint_timeout = 15min
wal_compression = on"
  write_file "$pg_snippet" 0644 root
  ROLLBACK[${#ROLLBACK[@]}]="撤销 PG 调优：rm -f $pg_snippet && systemctl restart postgresql"
  restart_svc postgresql
fi

# ═════════════════════════════════════════════════════════════════════
# 4. Redis 调优
# ═════════════════════════════════════════════════════════════════════
say "Redis 调优"
if [ ! -f /etc/redis/redis.conf ]; then
  skip "没找到 /etc/redis/redis.conf —— 跳过（先装 redis-server 或手动改）"
else
  redis_snippet="/etc/redis/redis.conf.d/99-stashbox-2g.conf"
  redis_pass="$(envfile_get REDIS_PASSWORD)"
  if [ -z "$redis_pass" ]; then
    warn "env 文件 $ENV_FILE 里没有 REDIS_PASSWORD —— **不写 requirepass**"
    warn "  → 无认证的 Redis = 同 VPC 内任意主机可读写限流状态 / 封禁状态 / 队列。"
    warn "  → 填完再跑一次本脚本即可补上。"
  fi

  CONTENT="# Stashbox 2 GiB 机器的 Redis 调优（由 tune-host.sh 生成）
# 这台机器上的 Redis 干三件事：缓存、配额/限流状态、Arq 蒸馏队列。
#
# maxmemory = 64mb
#   缓存 + 限流状态 + 队列项，实测空载 ~50MB。64MB 留了约 1.3 倍余量，
#   而且**必须设**：Redis 不设 maxmemory 的话，它会一直吃光系统内存，
#   把 PostgreSQL 和 Python 服务挤死（这台机器上的经典连锁反应）。
#   ⚠️ 这台机器物理内存只有 1613MB，比原假设的 2048MB 少 21%，
#      所以从 96mb 降到 64mb，与 cgroup 上限 128M 匹配。
#
# maxmemory-policy = volatile-lru
#   满了之后只淘汰**带 TTL 的**key。
#   本项目的缓存 key 全部带 TTL（quota 60s / article 300s / pending 60s），
#   所以先被淘汰的是缓存。
#   Arq 的队列 key 没有 TTL → 不会被淘汰 → **队列不会被内存回收吃掉**
#   （如果用 allkeys-lru / allkeys-random，队列项可能被无声丢掉，
#   表现为「任务进了队列但没人消费」）。
#   ⚠️ 如果哪天发现 Redis 报 OOM（没有可淘汰的 TTL key），
#      改成 noeviction —— 让写入显式失败，而不是丢数据。
maxmemory 64mb
maxmemory-policy volatile-lru
#
# save 900 1
#   每 15 分钟、且至少改动过 1 个 key 时做一次 RDB 快照。
#   快照一个 ~100MB 的数据集是毫秒级的，不影响服务。
# appendonly no
#   **不开 AOF**：AOF 会把每次写入都追加落盘，写放大小一半、AOF 文件比 RDB 大。
#   这台机器上的 Redis 数据是可重建的（权威数据在 PostgreSQL），
#   队列丢了最坏是重新触发一次蒸馏，不值得为它付这份 IO。
#   ⚠️ 代价：机器断电会丢掉最后一次快照之后的队列项。
save 900 1
appendonly no
rdbcompression yes
dbfilename dump.rdb
dir /var/lib/redis"
  if [ -n "$redis_pass" ]; then
    CONTENT="$CONTENT
#
# requirepass —— 密码来自 $ENV_FILE 的 REDIS_PASSWORD（不写死在这里）。
requirepass $redis_pass"
  fi
  write_file "$redis_snippet" 0640 root:redis
  # redis.conf 要真的 include 它；Redis 的 include 可以在任何位置，
  # 追加到文件末尾 = 我们的值覆盖前面的默认值。
  if ! grep -qE "^[[:space:]]*include[[:space:]]+.*redis\.conf\.d" /etc/redis/redis.conf 2>/dev/null; then
    append_line_once /etc/redis/redis.conf "include $redis_snippet"
  fi
  ROLLBACK[${#ROLLBACK[@]}]="撤销 Redis 调优：rm -f $redis_snippet && sed -i '/include .*redis.conf.d/d' /etc/redis/redis.conf && systemctl restart redis-server"
  restart_svc redis-server
fi

# ═════════════════════════════════════════════════════════════════════
# 5. swap
# ═════════════════════════════════════════════════════════════════════
say "swap（${SWAP_FILE}，${SWAP_SIZE}）"
# 为什么 2 GiB 机器上 swap 反而是**最重要的**保险：
#   没有 swap 时，一次意外的内存尖峰（LLM 大响应 + PG checkpoint 写回 +
#   ffmpeg 中间文件）会直接触发内核 OOM killer —— 它杀的是**内存占用最大的
#   那个进程**，运气不好就是 PostgreSQL，整个系统掉线且需要人工恢复。
#   有 swap 时，同样的尖峰会先变慢（几秒），然后回落 —— 用户最多觉得"卡了一下"。
#   代价：swap 是磁盘，慢了 1-2 个数量级。所以 swappiness 压到 10：
#   只有真的没内存了才用 swap，不把空闲页提前换出去。
if swapon --show 2>/dev/null | grep -qF "$SWAP_FILE"; then
  skip "$SWAP_FILE 已在用（跳过）"
elif [ -f "$SWAP_FILE" ]; then
  say "文件已存在但未启用 —— mkswap + swapon"
  run chmod 600 "$SWAP_FILE"
  run mkswap "$SWAP_FILE"
  run swapon "$SWAP_FILE"
else
  if ! fallocate -l "$SWAP_SIZE" "$SWAP_FILE" 2>/dev/null; then
    say "fallocate 不可用，退回 dd"
    run dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048 status=none
  fi
  run chmod 600 "$SWAP_FILE"
  run mkswap "$SWAP_FILE"
  run swapon "$SWAP_FILE"
fi
append_line_once /etc/fstab "$SWAP_FILE none swap sw 0 0"
ROLLBACK[${#ROLLBACK[@]}]="撤销 swap：swapoff $SWAP_FILE && rm -f $SWAP_FILE && sed -i '\\|$SWAP_FILE|d' /etc/fstab"

# ═════════════════════════════════════════════════════════════════════
# 6. journald 限容 + logrotate
# ═════════════════════════════════════════════════════════════════════
say "journald 限容"
journald_conf="/etc/systemd/journald.conf"
if [ -f "$journald_conf" ]; then
  # 5 个 unit 全部 StandardOutput=journal —— 不限容的话，
  # 日志会把 40GB 系统盘吃光（磁盘满 → PG 写不进去 → 整机停摆）。
  CONTENT="# Stashbox 2 GiB 机器的 journald 调优（由 tune-host.sh 生成）
#
# SystemMaxUse = 200M
#   journal 总体积上限。应用日志全走 journal（systemd unit 的
#   StandardOutput=journal），不限容的话 40GB 系统盘会被日志吃光。
#   200M 够看几天。
# SystemMaxFileSize = 50M
#   **单个** journal 文件的上限。总量有上限还不够：一个文件无限长的话，
#   排查时 tail/grep 会很难受，而且 journal 切文件时会有一次写放大。
#   journald 自带 zstd 压缩 → 「压缩归档」这一层它已经做了。
# SystemKeepFree = 512M
#   无论如何给磁盘留 512M 空闲 —— journald 宁可写不进去也不把盘占满。
# MaxRetentionSec = 30day
#   最多留 30 天。
# Storage = persistent
#   日志落盘，重启后还在（排查问题需要）。代价是占磁盘（被上面两项兜住）。
[Journal]
SystemMaxUse=200M
SystemMaxFileSize=50M
SystemKeepFree=512M
MaxRetentionSec=30day
Storage=persistent"
  write_file "$journald_conf" 0644 root
  ROLLBACK[${#ROLLBACK[@]}]="撤销 journald 调优：rm -f $journald_conf && systemctl restart systemd-journald"
  if [ "$DRY_RUN" -eq 0 ]; then
    systemctl restart systemd-journald || warn "systemd-journald 重启失败"
  fi
else
  skip "没找到 ${journald_conf}（跳过）"
fi

say "logrotate"
# 策略本身在 install-log-policy.sh 里（**唯一真相源**）——
# 它同时管我们自己的 /var/log/stashbox/*.log 和系统 nginx 那一段的「按大小」，
# 还要建日志目录。规则写两遍必然漂（nginx 那段有 maxsize、我们这段没有，
# 或者反过来），所以这里只调用，不复述内容。
#
# 为什么 nginx 也要动：它的 access_log 记的就是我们的 4 个 vhost
# （营销页 / API / 后台 / IP 兜底），而 APK 下载就在 /downloads ——
# 撞上爬虫猛打，一天能写几百 MB。系统策略只有天级轮转。
log_policy_script="$(dirname "$0")/install-log-policy.sh"
if [ -f "$log_policy_script" ]; then
  if [ "$DRY_RUN" -eq 0 ]; then
    bash "$log_policy_script" || warn "日志策略安装失败（logrotate 仍按原样工作）"
  else
    printf '    [dry-run] bash %s\n' "$log_policy_script"
  fi
  ROLLBACK[${#ROLLBACK[@]}]="撤销 logrotate 策略：rm -f /etc/logrotate.d/stashbox（nginx 那段从 /root/nginx-config-backups/nginx.bak.* 恢复）"
else
  warn "找不到 $log_policy_script —— 跳过 logrotate 策略安装"
fi

# ═════════════════════════════════════════════════════════════════════
# 7. sysctl + MALLOC_ARENA_MAX
# ═════════════════════════════════════════════════════════════════════
say "sysctl"
CONTENT="# Stashbox 2 GiB 机器的内核调优（由 tune-host.sh 生成）
#
# vm.swappiness = 10
#   换页积极度（0-100）。默认 60 意味着内核会**主动**把冷内存页换到 swap ——
#   在 2 GiB 机器上这等于慢性自杀：swap 一慢，所有访问过的内存都变慢，
#   而内存其实还够用。压到 10 = 只有真的缺内存了才换。
#   （这**不**意味着 swap 没用 —— swap 是尖峰时的保险，见上面的 swap 章节。）
#
# vm.dirty_ratio = 5 / vm.dirty_background_ratio = 3
#   脏页（改了还没写盘的内存）上限：默认 20/10 在 2 GiB 机器上 =
#   400MB/200MB 的脏页。一次大查询或 PG checkpoint 能把脏页顶到上限，
#   内核随后进入 direct reclaim（同步刷盘）—— 表现为整机卡死几秒。
#   压到 5/3（100MB/60MB）让刷盘更早、更平缓地发生。
#
# 其余内核默认值在 2 GiB 机器上都够用，没有动。
vm.swappiness = 10
vm.dirty_ratio = 5
vm.dirty_background_ratio = 3"
write_file /etc/sysctl.d/99-stashbox-2g.conf 0644 root
if [ "$DRY_RUN" -eq 0 ]; then
  sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-stashbox-2g.conf || warn "sysctl 应用失败"
else
  printf '    [dry-run] sysctl --system\n'
fi
ROLLBACK[${#ROLLBACK[@]}]="撤销 sysctl：rm -f /etc/sysctl.d/99-stashbox-2g.conf && sysctl --system"

say "MALLOC_ARENA_MAX"
# ⚠️ MALLOC_ARENA_MAX 是**环境变量**，不是 sysctl —— 内核没有这个旋钮。
# glibc 会给每个线程分配独立的 malloc arena（每个 64MB 虚拟、实际按需占用）。
# 线程一多（uvicorn 的线程池 + asyncpg + ffmpeg），RSS 就能凭空涨几百 MB。
# 压到 2 之后，多线程进程的 RSS 基本贴着真实用量走。
# 写进 /etc/environment 是给**登录 shell / PAM 会话**用的；
# systemd 启动的服务不读 /etc/environment —— 单元里已经显式写了
# Environment=MALLOC_ARENA_MAX=2（见 deploy/systemd/*.service）。
if grep -q '^MALLOC_ARENA_MAX=' /etc/environment 2>/dev/null; then
  skip "/etc/environment 已有 MALLOC_ARENA_MAX（跳过）"
else
  append_line_once /etc/environment "MALLOC_ARENA_MAX=2"
  ROLLBACK[${#ROLLBACK[@]}]="撤销 MALLOC_ARENA_MAX：sed -i '/^MALLOC_ARENA_MAX=/d' /etc/environment"
fi

# ═════════════════════════════════════════════════════════════════════
# 8. cgroup 内存上限（PG / Redis / nginx）
# ═════════════════════════════════════════════════════════════════════
say "cgroup 内存上限"
# 5 个 Python 单元的 MemoryMax 之和是 1088M（见 deploy/systemd/README.md）。
# 这三个不是我们的单元，但它们同样在这台机器上 —— 不给上限的话，
# PG 连接数暴涨就能把 2 GiB 吃光，Python 服务一个都活不下来。
# 这三个上限之和：448 + 192 + 64 = 704M。
# 合计：1088 + 704 + OS/journald ~150 = 1942M < 2048M ✓
#
# ⚠️ PostgreSQL 的 MemoryMax 触顶 = PG 被 OOM kill = 数据面掉线。
#    448M 是按「shared_buffers 96MB + 40 条连接 × 6MB + 进程开销」估的，
#    留了约 25% 余量。真不够用时先降 max_connections，别直接抬这个值。
write_cgroup_cap() {
  local unit="$1" high="$2" max="$3" comment="$4"
  # ⚠️ drop-in 目录名必须是**完整单元名 + .d**（`redis-server.service.d`），
  #    **不能**省略 `.service`（写成 `redis-server.d` 会被 systemd 静默忽略 ——
  #    `systemctl show <unit> -p DropInPaths` 返回空，memory.max 仍是 max，
  #    看起来"配了"其实一点作用都没有）。
  local dir="/etc/systemd/system/${unit}.service.d"
  local file="$dir/10-stashbox-2g.conf"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] 写 %s（%s / %s）\n' "$file" "$high" "$max"
    return 0
  fi
  # 单元存在性检测。
  # ⚠️ **只能用 systemctl cat，不能用 systemctl list-unit-files**：
  #    `list-unit-files <name>` 把参数当 glob 模式，精确名（非模板）
  #    会返回非 0 —— `list-unit-files postgresql` 直接 FAIL，
  #    而 `systemctl cat postgresql` 是 OK 的。
  #    用错判据的后果：PG/Redis/nginx 的 cgroup 上限**全被跳过**，
  #    而脚本还打印「不在这台机器上」——看起来正常，实际毫无限制，
  #    内存账本形同虚设。
  if ! systemctl cat "$unit" >/dev/null 2>&1; then
    skip "$unit 不在这台机器上（跳过 cgroup 上限）"
    return 0
  fi
  mkdir -p "$dir"
  CONTENT="# $comment
# 由 tune-host.sh 生成。删掉这个文件即可回滚。
[Service]
MemoryHigh=$high
MemoryMax=$max"
  printf '%s\n' "$CONTENT" > "$file"
  chmod 0644 "$file"
  say "写入 ${file}（${high} / ${max}）"
  ROLLBACK[${#ROLLBACK[@]}]="撤销 $unit 的 cgroup 上限：rm -f $file && systemctl daemon-reload && systemctl restart $unit"
}

# ⚠️ PostgreSQL 的单元名特殊情况：
#    `postgresql.service` 是个 Type=oneshot 的**元单元**（ExecStart=/bin/true），
#    真正跑的是模板实例 `postgresql@16-main.service`。
#    挂在元单元上的 drop-in 对实际进程**毫无作用**（实测 memory.max 仍为 max），
#    必须挂到**模板**上：/etc/systemd/system/postgresql@.service.d/
#    —— 模板 drop-in 会被所有 postgresql@<ver>-<cluster> 实例继承。
#
# 这条很容易踩：unit 存在（有 systemctl cat）、drop-in 写成功、
# 但限制根本没生效 —— 只有实际去读 /sys/fs/cgroup/.../memory.max 才能发现。
write_cgroup_cap_template() {
  local template="$1" high="$2" max="$3" comment="$4"
  # template 传的是**模板名带 @**（如 postgresql@），完整单元名是
  # `postgresql@.service`，drop-in 目录就是 `postgresql@.service.d`。
  # ⚠️ 调用时**必须**传 `postgresql@` 而不是 `postgresql` ——
  #    传错会得到 `postgresql.service.d`，挂到 oneshot 元单元上，
  #    对实际跑的 postgresql@16-main 实例毫无作用（memory.max 仍是 max）。
  local dir="/etc/systemd/system/${template}.service.d"
  local file="$dir/10-stashbox-2g.conf"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] 写 %s（%s / %s）\n' "$file" "$high" "$max"
    return 0
  fi
  if ! systemctl cat "$template" >/dev/null 2>&1; then
    skip "${template}.service 模板不存在（跳过 cgroup 上限）"
    return 0
  fi
  mkdir -p "$dir"
  CONTENT="# $comment
# 由 tune-host.sh 生成。删掉这个文件即可回滚。
# ⚠️ 挂在**模板**上 —— postgresql.service 本身是 oneshot 元单元，
#    真正跑的是 postgresql@<ver>-<cluster> 实例，限制必须挂模板才生效。
[Service]
MemoryHigh=$high
MemoryMax=$max"
  printf '%s\n' "$CONTENT" > "$file"
  chmod 0644 "$file"
  say "写入 ${file}（${high} / ${max}）"
  ROLLBACK[${#ROLLBACK[@]}]="撤销 ${template} 的 cgroup 上限：rm -f $file && systemctl daemon-reload && systemctl restart postgresql"
}

# ⚠️ 实际内存不是 2048M。
#
# 轻量应用服务器标称 2 GiB，但**物理内存只有 1613MB**
# （free -m 实测：Mem total 1613 / available 1247；云厂商把一部分
#  内存划给 hypervisor / 缓存，所以 guest 看到的是 1613 而不是 2048）。
#
# 如果照着 2048M 的预算表走（硬上限合计 1920M），
# 5 个单元 + PG + Redis 同时顶满时，**cgroup 上限之和 > 物理内存**：
# 内核会先吃光物理内存 + swap，然后在 cgroup 里触发 OOM killer，
# 被杀的可能是 PostgreSQL（内存占用最大那个）→ 整机数据面掉线。
#
# 目标：硬上限之和 ≤ 物理内存 × 0.85，给 OS/journald/page cache 留够。
#   实测 1613M × 0.85 ≈ 1371M
#   分配：Python 五单元 856M + PG 320M + Redis 128M + nginx 48M = 1352M ✓
#
# PG 从 448M 降到 320M：shared_buffers 从 96MB 降到 64MB，
# max_connections 从 40 降到 30（内测并发用不到 40 条连接）。
# Redis 从 192M 降到 128M：maxmemory 96mb → 64mb。
#
# ⚠️ 真不够用时的正确顺序：先降 PG 的 max_connections 和 shared_buffers，
#    再降 Redis maxmemory，**最后**才动 Python 单元的 MemoryMax ——
#    PG 被 OOM kill 的代价（数据面全断）远大于单个 Python 单元重启。
write_cgroup_cap_template 'postgresql@' 224M 320M "PostgreSQL 内存上限（1613MB 实测机器）"
write_cgroup_cap redis-server 96M 128M "Redis 内存上限（1613MB 实测机器）"
write_cgroup_cap nginx 24M 48M "nginx 内存上限（1613MB 实测机器）"

if [ "$DRY_RUN" -eq 0 ]; then
  systemctl daemon-reload
  # ── 验证 drop-in 真的生效 ─────────────────────────────────────────
  #
  # ⚠️ 为什么要多这一步：`systemctl show -p MemoryMax` 读的是**单元配置**，
  #    而单元配置生效 ≠ 进程真被限制。真正的事实藏在
  #    /sys/fs/cgroup/<cgroup>/memory.max 里。
  #    历史上在这里踩过两次：
  #      1) drop-in 目录名写成 `redis-server.d`（漏了 .service）→ 静默忽略
  #      2) PG 的限制挂在 oneshot 元单元上 → 对模板实例无效
  #    两种情况 systemctl show 都显示配置对，只有读 cgroup 才能发现没生效。
  say "验证 cgroup 限制是否真正生效"
  for _u in postgresql@16-main redis-server nginx; do
    _cg="$(systemctl show "$_u" -p ControlGroup --value 2>/dev/null)"
    if [ -z "$_cg" ]; then
      warn "$_u —— 拿不到 ControlGroup（单元未运行？），跳过校验"
      continue
    fi
    _f="/sys/fs/cgroup${_cg}/memory.max"
    if [ ! -f "$_f" ]; then
      warn "$_u —— 找不到 $_f（cgroup 未挂载？），跳过校验"
      continue
    fi
    _v="$(cat "$_f" 2>/dev/null)"
    if [ "$_v" = "max" ]; then
      warn "$_u —— memory.max 仍是 max（无限）！drop-in 可能挂错单元/目录名"
    else
      info "$_u —— memory.max = $((_v / 1024 / 1024)) MB ✅"
    fi
  done
fi

# ═════════════════════════════════════════════════════════════════════
# 9. 防火墙状态（只检查 + 提示，不自动改）
# ═════════════════════════════════════════════════════════════════════
# ⚠️ 8100 是**内网 API 端口**，只有本机的 nginx 能碰它。
#    把它开到公网 = 任何人都能绕过 TLS 终止直接打 API，还能自己伪造
#    X-Forwarded-For 绕过登录限流 —— 这正是 common/ip.py 文档里说的
#    "生产上只要 8100 不对公网开放，伪造面就只剩同一台机器上的其他进程"。
#    8101/8102/8103 同理（它们连鉴权都不在网关这一层）。
#    5432（PG）和 6379（Redis）更不能开：无认证/弱认证的内网服务，
#    暴露在公网上等于把库和限流状态交出去。
#
# 公网上**只应该**有：22（SSH）、80（HTTP，只做跳转 + ACME）、443（HTTPS）。
#
# 这一段**只读**：它不替你改防火墙（自动改防火墙有可能把自己锁在 SSH 外面）。
# 改法给你，贴进阿里云控制台或 ufw 即可。
say "防火墙状态"
firewall_open_ports=""
if have ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
  firewall_open_ports="$(ufw status 2>/dev/null | grep -E '(^|[^0-9])(8100|8101|8102|8103|5432|6379)(/tcp|[^0-9]|$)' || true)"
  if [ -n "$firewall_open_ports" ]; then
    warn "ufw 里发现了对公网开放的内部端口 —— 必须关掉："
    printf '%s\n' "$firewall_open_ports" | sed 's/^/      /' >&2
    warn "  修法：ufw deny 8100/tcp && ufw deny 8101/tcp && ufw deny 8102/tcp && ufw deny 8103/tcp"
    warn "        ufw deny 5432/tcp && ufw deny 6379/tcp"
  else
    info "ufw：没发现对公网开放的内部端口"
  fi
  info "ufw 允许的公网端口应当只有：22/tcp、80/tcp、443/tcp"
elif have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
  warn "本机用的是 firewalld —— 阿里云轻量服务器一般用 ufw 或控制台防火墙"
  warn "  请确认公网只放行 22/80/443；8100-8103、5432、6379 必须不开放"
else
  info "本机没有启用 ufw/firewalld —— 请在**阿里云控制台的防火墙**里确认："
  info "  只放行 22/80/443；8100-8103、5432、6379 **不要**放行"
fi

# 服务实际绑在哪个地址上。绑 0.0.0.0 = 只要防火墙漏了一条口子就暴露。
if have ss; then
  exposed_listeners="$(ss -ltn 2>/dev/null \
    | awk 'NR>1 {print $4}' | grep -E ':(8100|8101|8102|8103)$' | grep -vE '^(127\.0\.0\.1|\[::1\])' || true)"
  if [ -n "$exposed_listeners" ]; then
    warn "内部端口监听在非回环地址上（$(printf '%s' "$exposed_listeners" | tr '\n' ' ')）——"
    warn "  这些端口只该本机 nginx 能访问。把 STASHBOX_BIND_HOST 留成默认的 127.0.0.1"
    warn "  （run-service.sh 默认就是 127.0.0.1），或检查是不是别的进程占着这些端口。"
  else
    info "8100-8103 都没有监听在非回环地址上（只在 127.0.0.1）"
  fi
else
  skip "没有 ss 命令，跳过监听地址检查（apt install iproute2 即可）"
fi

# ═════════════════════════════════════════════════════════════════════
# 10. 摘要
# ═════════════════════════════════════════════════════════════════════
echo
echo "════════════════════════════════════════════════════════════════════"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  dry-run 结束 —— 上面这些**都没有**真的执行。"
else
  echo "  调优完成。下面是改了什么、怎么回滚。"
fi
echo "════════════════════════════════════════════════════════════════════"
# 内存账本：**从实际配置动态读**，不写死数字。
# 写死的后果：改了 unit 的 MemoryMax 却忘了改这里，
# 账本显示「余量 106M」而实际已经超物理内存 —— 看起来安全，实际会 OOM。
echo "  内存分配账（物理内存实测 ${PHYS_MEM_MB}M）："
_py_sum=0
for _u in api-gateway user-service content-service ai-service ai-worker; do
  _v="$(grep -h '^MemoryMax=' "${SCRIPT_DIR}/systemd/stashbox-${_u}.service" 2>/dev/null | head -1 | sed 's/[^0-9]//g')"
  [ -n "$_v" ] && _py_sum=$((_py_sum + _v))
done
_pg_max="$(grep -h '^MemoryMax=' /etc/systemd/system/postgresql@.service.d/10-stashbox-2g.conf 2>/dev/null | head -1 | sed 's/[^0-9]//g')"
_redis_max="$(grep -h '^MemoryMax=' /etc/systemd/system/redis-server.service.d/10-stashbox-2g.conf 2>/dev/null | head -1 | sed 's/[^0-9]//g')"
_nginx_max="$(grep -h '^MemoryMax=' /etc/systemd/system/nginx.service.d/10-stashbox-2g.conf 2>/dev/null | head -1 | sed 's/[^0-9]//g')"
[ -z "$_pg_max" ] && _pg_max=0
[ -z "$_redis_max" ] && _redis_max=0
[ -z "$_nginx_max" ] && _nginx_max=0
echo "    5 个 Python 单元 MemoryMax 之和   ${_py_sum}M"
echo "    PostgreSQL  cgroup 上限            ${_pg_max}M"
echo "    Redis      cgroup 上限            ${_redis_max}M"
echo "    nginx      cgroup 上限             ${_nginx_max}M"
echo "    OS + systemd + journald          ~150M"
echo "    ─────────────────────────────────────"
_total=$((_py_sum + _pg_max + _redis_max + _nginx_max + 150))
echo "    合计                             ${_total}M / ${PHYS_MEM_MB}M  （余量 $((PHYS_MEM_MB - _total))M）"
if [ "$_total" -gt "$PHYS_MEM_MB" ]; then
  printf '  ! ⚠️ 硬上限之和已超过物理内存！超出的部分会触发 cgroup OOM killer。\n' >&2
  printf '  !    请调低 deploy/systemd/*.service 的 MemoryMax。\n' >&2
fi
echo
echo "  回滚："
if [ ${#ROLLBACK[@]} -eq 0 ]; then
  echo "    （没有改动）"
else
  for line in "${ROLLBACK[@]}"; do
    echo "    - $line"
  done
fi
echo
echo "  还需要你手工做的："
echo "    1) 重启 PostgreSQL / Redis 让新配置生效（shared_buffers 等是 postmaster 参数）："
echo "         systemctl restart postgresql redis-server"
echo "    2) 填环境变量（JWT_SECRET / 密码 / OSS key）："
echo "         install -m 0640 -o root -g $SERVICE_USER .env.production.2g.example $CONF_DIR/stashbox.env"
echo "         vi $CONF_DIR/stashbox.env"
echo "       ⚠️ 先填 REDIS_PASSWORD 再跑一次本脚本 —— redis.conf 的 requirepass 是从"
echo "          这个文件里读的，顺序反了 Redis 就是无认证的。"
echo "    3) 建库建号（PostgreSQL 本机实例）："
echo "         sudo -u postgres psql -c \"CREATE ROLE stashbox LOGIN PASSWORD '<你的密码>'\""
echo "         sudo -u postgres createdb -O stashbox stashbox"
echo "    4) 装 systemd 单元并启动（见 deploy/systemd/README）"
echo "    5) 预检：backend/deploy/preflight.sh $CONF_DIR/stashbox.env"
echo "    6) 防火墙：公网只放行 22/80/443。8100-8103、5432、6379 **不要**放行"
echo "       （脚本只检查、不自动改 —— 详见上面第 9 段的说明）。"
echo
