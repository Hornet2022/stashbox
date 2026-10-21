#!/usr/bin/env bash
#
# Stashbox 生产部署预检（preflight）
# =================================
#
# 目的：把"内测发出去才发现配错了"变成"部署前 5 秒就知道"。
# 任何一项 FAIL → exit 1，不许发版。
#
# 用法：
#     ./preflight.sh                       # 读同目录的 .env.production
#     ./preflight.sh /path/to/.env.prod    # 指定 env 文件
#     SKIP_NETWORK=1 ./preflight.sh        # 跳过 DB/Redis 连通性检查（离线校验）
#
# 环境变量优先级：真实环境变量 > env 文件（同 common/config.py 的 override=False 语义）。
#
# 设计约束：
#   · 只读，不改任何东西（不改 .env、不改数据库、不重启服务）
#   · 不依赖 jq / yq / python（python3 有就用，没有就降级跳过某一项检查）
#   · bash 3.2 兼容（macOS 自带 bash 就能跑）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${1:-$SCRIPT_DIR/.env.production}"

# ── 输出缓冲 ──────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
LINES=()

pass() { PASS_COUNT=$((PASS_COUNT + 1)); LINES[${#LINES[@]}]="PASS|$1|$2"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); LINES[${#LINES[@]}]="FAIL|$1|$2"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); LINES[${#LINES[@]}]="WARN|$1|$2"; }
skip() { LINES[${#LINES[@]}]="SKIP|$1|$2"; }

# ── 读变量：真实环境变量优先，其次 env 文件 ───────────────────────────
# 用 awk 解析而不是 eval/source —— env 文件是配置，不是 shell 脚本，
# source 它等于允许配置文件里跑任意命令。
envfile_get() {
  local key="$1"
  awk -v wanted="$key" '
    BEGIN { res = "" }
    {
      line = $0
      # 去掉行首空白
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
      # 去一层引号
      if (length(val) >= 2) {
        q = substr(val, 1, 1)
        if ((q == "\"" || q == "\047") && substr(val, length(val), 1) == q) {
          val = substr(val, 2, length(val) - 2)
        }
      }
      # 未加引号时，去掉 " #注释"（docker compose env_file 同规则）
      if (substr(val, 1, 1) != "\"" && substr(val, 1, 1) != "\047") {
        h = index(val, " #")
        if (h > 0) val = substr(val, 1, h - 1)
      }
      res = val
    }
    END { if (res != "") printf "%s", res }
  ' "$ENV_FILE" 2>/dev/null
}

v() {
  local key="$1"
  local from_env
  from_env="$(printenv "$key" 2>/dev/null || true)"
  if [ -n "$from_env" ]; then
    printf '%s' "$from_env"
    return 0
  fi
  if [ -f "$ENV_FILE" ]; then
    envfile_get "$key"
  fi
}

# ── 小工具 ────────────────────────────────────────────────────────────
is_placeholder() {
  # 占位符 = 一定会让人以为配好了、其实没配的值。
  # 判定必须**精确**：早期版本用子串匹配（*xxxx*），结果把合法的阿里云实例名
  # （r-xxxx.redis.rds.aliyuncs.com）也判成占位符 —— 误报比漏报更招人烦。
  case "$1" in
    "") return 0 ;;
    xxx|XXX|xxxx|XXXX|xxxxxx|xxxxxxxx) return 0 ;;
    changeme|CHANGEME|change-me|CHANGE-ME|change_me) return 0 ;;
    *changeme*|*CHANGEME*) return 0 ;;
    placeholder|PLACEHOLDER) return 0 ;;
    todo|TODO) return 0 ;;
    your-*|your_*|YOUR-*) return 0 ;;
    sk-xxx|sk-XXXX) return 0 ;;
    "<"*|">"*) return 0 ;;
    *) return 1 ;;
  esac
}

tcp_probe() {
  # 只证明端口开着（不证明认证能过）。bash 内建 /dev/tcp，无需 nc。
  local host="$1" port="$2"
  (exec 3<>"/dev/tcp/$host/$port") >/dev/null 2>&1
}

have() { command -v "$1" >/dev/null 2>&1; }

# 算出 alembic 的 head（没有其它迁移指向它的那个 revision）。
# 用 awk 而不是 python：macOS 自带的 awk（BWK）没有 ENDFILE，
# 所以靠 FNR==1 换文件时把上一个文件结算掉。
alembic_head() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  awk '
    FNR == 1 {
      if (prev != "") { revs[prev] = 1; if (prevdown != "") downs[prevdown] = 1 }
      prev = ""; prevdown = ""
    }
    /^revision[^=]*=/ { if (match($0, /"[^"]+"/)) prev = substr($0, RSTART + 1, RLENGTH - 2) }
    /^down_revision/ { if (match($0, /"[^"]+"/)) prevdown = substr($0, RSTART + 1, RLENGTH - 2) }
    END {
      if (prev != "") { revs[prev] = 1; if (prevdown != "") downs[prevdown] = 1 }
      n = 0
      for (r in revs) if (!(r in downs)) { head = r; n++ }
      if (n == 1) print head
    }
  ' "$dir"/*.py 2>/dev/null
}

# ═════════════════════════════════════════════════════════════════════
# 0. 前置：env 文件
# ═════════════════════════════════════════════════════════════════════
if [ ! -f "$ENV_FILE" ]; then
  printf '\033[31m✗ 找不到 env 文件：%s\033[0m\n' "$ENV_FILE"
  printf '  先做：cp %s/.env.production.example %s\n' "$SCRIPT_DIR" "$ENV_FILE"
  exit 1
fi
printf '▶ env 文件：%s\n' "$ENV_FILE"

# ═════════════════════════════════════════════════════════════════════
# 1. 运行环境
# ═════════════════════════════════════════════════════════════════════
env_val="$(v ENVIRONMENT)"
if [ "$env_val" = "prod" ]; then
  pass "ENVIRONMENT" "prod"
elif [ -z "$env_val" ]; then
  fail "ENVIRONMENT" "未设置 —— 生产必须是 prod"
else
  fail "ENVIRONMENT" "当前是 '$env_val'，生产必须是 prod"
fi

dbg="$(v DEBUG)"
if [ "$dbg" = "false" ]; then
  pass "DEBUG" "false"
elif [ -z "$dbg" ]; then
  # common/config.py 里 debug 的代码默认值是 True —— 不显式关掉就是开着 SQL echo。
  warn "DEBUG" "未设置 —— 代码默认是 true（会把每条 SQL 打进日志）。请显式写 DEBUG=false"
else
  fail "DEBUG" "当前是 '$dbg' —— 生产必须 false（true 会把每条 SQL 打进日志）"
fi

# ═════════════════════════════════════════════════════════════════════
# 2. 密钥与后门
# ═════════════════════════════════════════════════════════════════════
jwt="$(v JWT_SECRET)"
if [ -z "$jwt" ]; then
  fail "JWT_SECRET" "未设置 —— 网关签发的所有 token 都不可用"
elif [ "$jwt" = "dev-secret-change-me-in-production" ]; then
  fail "JWT_SECRET" "还是 dev 默认值 —— 任何人可伪造 token"
elif [ "${#jwt}" -lt 16 ]; then
  fail "JWT_SECRET" "只有 ${#jwt} 字符（< 16），代码会直接拒绝启动"
else
  pass "JWT_SECRET" "已设置（${#jwt} 字符）"
fi

admin_pw="$(v ADMIN_BOOTSTRAP_PASSWORD)"
if [ -z "$admin_pw" ]; then
  fail "ADMIN_BOOTSTRAP_PASSWORD" "未设置 —— 迁移 0015 会用公开的 dev 密码 'admin@stashbox123' 给 admin 账号"
elif [ "$admin_pw" = "admin@stashbox123" ]; then
  fail "ADMIN_BOOTSTRAP_PASSWORD" "还是 dev 默认值 —— 任何人都能登录 admin-web"
elif [ "${#admin_pw}" -lt 12 ]; then
  warn "ADMIN_BOOTSTRAP_PASSWORD" "只有 ${#admin_pw} 字符，建议 >= 12"
else
  pass "ADMIN_BOOTSTRAP_PASSWORD" "已设置（${#admin_pw} 字符）"
fi

# 这两个是"存在即生效"的后门开关，连写 0 都会打开 —— 只能检查"没配"
for devflag in STASHBOX_ALLOW_DEV_JWT STASHBOX_ALLOW_DEV_TOKEN; do
  dv="$(v "$devflag")"
  if [ -n "$dv" ]; then
    fail "$devflag" "已设置（值='$dv'）—— 这是生产后门开关，必须删掉（连写 0 都会生效）"
  else
    pass "$devflag" "未设置（正确）"
  fi
done

# ═════════════════════════════════════════════════════════════════════
# 3. 跨域白名单（网关 prod 启动门禁）
# ═════════════════════════════════════════════════════════════════════
cors="$(v STASHBOX_CORS_ALLOW_ORIGINS)"
if [ -z "$cors" ]; then
  fail "STASHBOX_CORS_ALLOW_ORIGINS" "未设置 —— 网关在 prod 下会直接启动失败"
elif [ "$cors" = "*" ]; then
  fail "STASHBOX_CORS_ALLOW_ORIGINS" "是 '*' —— 等于把运营后台 cookie 暴露给任意站点"
else
  pass "STASHBOX_CORS_ALLOW_ORIGINS" "$(printf '%s' "$cors" | tr ',' '\n' | grep -c . || true) 个 origin"
fi

# ═════════════════════════════════════════════════════════════════════
# 4. 代理信任（限流按真实客户端 IP 计数）
# ═════════════════════════════════════════════════════════════════════
proxy_ips="$(v TRUSTED_PROXY_IPS)"
if [ -z "$proxy_ips" ]; then
  fail "TRUSTED_PROXY_IPS" "未设置 —— 网关拿不到真实客户端 IP，登录限流会退化成按网关 IP 计数"
elif [ "$proxy_ips" = "*" ]; then
  fail "TRUSTED_PROXY_IPS" "是 '*' —— 公网客户端可伪造 X-Forwarded-For 绕过限流"
else
  pass "TRUSTED_PROXY_IPS" "$proxy_ips"
fi

pub_url="$(v PUBLIC_GATEWAY_URL)"
if [ -z "$pub_url" ]; then
  fail "PUBLIC_GATEWAY_URL" "未设置 —— 客户端拿到的 URL 会指向内网/localhost"
else
  pass "PUBLIC_GATEWAY_URL" "$pub_url"
fi

# ═════════════════════════════════════════════════════════════════════
# 5. 数据库
# ═════════════════════════════════════════════════════════════════════
pg_host="$(v POSTGRES_HOST)"
pg_port="$(v POSTGRES_PORT)"; pg_port="${pg_port:-5432}"
pg_user="$(v POSTGRES_USER)"
pg_pass="$(v POSTGRES_PASSWORD)"
pg_db="$(v POSTGRES_DB)"

for kv in "POSTGRES_HOST:$pg_host" "POSTGRES_USER:$pg_user" "POSTGRES_PASSWORD:$pg_pass" "POSTGRES_DB:$pg_db"; do
  key="${kv%%:*}"; val="${kv#*:}"
  if is_placeholder "$val"; then
    fail "$key" "未设置或仍是占位符"
  else
    pass "$key" "$val"
  fi
done

# 连通性
if [ "${SKIP_NETWORK:-0}" = "1" ]; then
  skip "PostgreSQL 连通" "SKIP_NETWORK=1，已跳过"
elif [ -z "$pg_host" ]; then
  skip "PostgreSQL 连通" "没有 POSTGRES_HOST，跳过"
elif tcp_probe "$pg_host" "$pg_port"; then
  if have psql; then
    if PGPASSWORD="$pg_pass" psql -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" \
         -tAc "SELECT 1" >/dev/null 2>&1; then
      pass "PostgreSQL 连通" "$pg_host:$pg_port/${pg_db}（认证 + 查询 OK）"
    else
      fail "PostgreSQL 连通" "$pg_host:$pg_port —— 认证或查询失败（账号/密码/白名单）"
    fi
  else
    pass "PostgreSQL 连通" "$pg_host:$pg_port 端口可达（未装 psql，只验了 TCP）"
  fi
else
  fail "PostgreSQL 连通" "$pg_host:$pg_port 端口不可达"
fi

# ═════════════════════════════════════════════════════════════════════
# 6. Redis
# ═════════════════════════════════════════════════════════════════════
redis_host="$(v REDIS_HOST)"
redis_port="$(v REDIS_PORT)"; redis_port="${redis_port:-6379}"
redis_db="$(v REDIS_DB)"; redis_db="${redis_db:-0}"
redis_pass="$(v REDIS_PASSWORD)"
redis_url="$(v REDIS_URL)"

if is_placeholder "$redis_host"; then
  fail "REDIS_HOST" "未设置或仍是占位符"
else
  pass "REDIS_HOST" "$redis_host"
fi

if [ -z "$redis_pass" ]; then
  fail "REDIS_PASSWORD" "未设置 —— 托管 Redis 无认证 = VPC 内任意 Pod 可读写（限流/缓存/封禁状态都在里面）"
else
  pass "REDIS_PASSWORD" "已设置"
fi

# REDIS_URL 与 REDIS_* 必须指向同一个实例（worker 只读 REDIS_URL）
if [ -z "$redis_url" ]; then
  fail "REDIS_URL" "未设置 —— Arq worker 只读这个变量，缺了会静默连 localhost，任务永远没人消费"
else
  mismatch=""
  case "$redis_url" in
    *"@$redis_host"*) ;;
    *) mismatch="host（URL 与 REDIS_HOST 不一致）" ;;
  esac
  case "$redis_url" in
    *":$redis_port/"*) ;;
    *) mismatch="${mismatch:+$mismatch, }port（URL 与 REDIS_PORT 不一致）" ;;
  esac
  case "$redis_url" in
    *"/$redis_db"*) ;;
    *) mismatch="${mismatch:+$mismatch, }db（URL 与 REDIS_DB 不一致）" ;;
  esac
  if [ -n "$redis_pass" ]; then
    case "$redis_url" in
      *":$redis_pass@"*) ;;
      *) mismatch="${mismatch:+$mismatch, }password（URL 与 REDIS_PASSWORD 不一致）" ;;
    esac
  fi
  if [ -n "$mismatch" ]; then
    fail "REDIS_URL 一致性" "与 REDIS_* 不一致：${mismatch}（worker 和 web 会连到不同实例）"
  else
    pass "REDIS_URL 一致性" "与 REDIS_HOST/PORT/DB/PASSWORD 一致"
  fi
fi

if [ "${SKIP_NETWORK:-0}" = "1" ]; then
  skip "Redis 连通" "SKIP_NETWORK=1，已跳过"
elif [ -z "$redis_host" ]; then
  skip "Redis 连通" "没有 REDIS_HOST，跳过"
elif tcp_probe "$redis_host" "$redis_port"; then
  if have redis-cli; then
    if REDISCLI_AUTH="$redis_pass" redis-cli -h "$redis_host" -p "$redis_port" -n "$redis_db" PING 2>/dev/null | grep -q PONG; then
      pass "Redis 连通" "$redis_host:$redis_port/${redis_db}（认证 + PING OK）"
    else
      fail "Redis 连通" "$redis_host:$redis_port —— 认证或 PING 失败"
    fi
  else
    pass "Redis 连通" "$redis_host:$redis_port 端口可达（未装 redis-cli，只验了 TCP）"
  fi
else
  fail "Redis 连通" "$redis_host:$redis_port 端口不可达"
fi

# ═════════════════════════════════════════════════════════════════════
# 7. 对象存储（生产必须 OSS）
# ═════════════════════════════════════════════════════════════════════
storage="$(v STORAGE_PROVIDER)"
if [ -z "$storage" ]; then
  storage="oss"   # 代码默认值
fi
if [ "$storage" = "oss" ]; then
  for kv in "OSS_ENDPOINT:$(v OSS_ENDPOINT)" "OSS_BUCKET:$(v OSS_BUCKET)" \
            "OSS_ACCESS_KEY_ID:$(v OSS_ACCESS_KEY_ID)" "OSS_ACCESS_KEY_SECRET:$(v OSS_ACCESS_KEY_SECRET)"; do
    key="${kv%%:*}"; val="${kv#*:}"
    if is_placeholder "$val"; then
      fail "$key" "未设置或仍是占位符（STORAGE_PROVIDER=oss 时必填）"
    else
      pass "$key" "已设置"
    fi
  done
  oss_region="$(v OSS_REGION)"
  if [ -z "$oss_region" ]; then
    warn "OSS_REGION" "未设置 —— 代码默认 us-east-1，对阿里云 OSS 是错的（签名会失败）"
  else
    pass "OSS_REGION" "$oss_region"
  fi

  # 地域一致性：endpoint 与 region 必须指向同一个地域，否则签名直接失败。
  # 内网端点还有更硬的前提 —— 轻量服务器与 bucket 必须同地域，
  # 不同地域时 -internal 域名虽然能解析（解析到 100.118.x.x 服务网段），
  # 但**流量走不出去**（实测：武汉机器上 curl oss-cn-hangzhou-internal:443 直接超时）。
  oss_endpoint="$(v OSS_ENDPOINT)"
  if [ -n "$oss_region" ] && [ -n "$oss_endpoint" ]; then
    # 去掉 "-internal" 后缀和域名部分，只留地域段。
    # 不用 sed 贪婪匹配：oss-cn-hangzhou-internal 会被 [a-z0-9-]* 整体吞掉。
    ep_region="${oss_endpoint%-internal*}"
    ep_region="${ep_region%%.*}"
    # 端点地域是 oss-cn-xxx 形式，region 变量是 oss-cn-xxx；
    # 但机器元数据给的是 cn-xxx（没有 oss- 前缀），比对时要归一化。
    ep_short="${ep_region#oss-}"
    region_short="${oss_region#oss-}"
    if [ -z "$ep_region" ]; then
      warn "OSS 地域一致性" "无法从 OSS_ENDPOINT='$oss_endpoint' 解析出地域（自定义域名/CNAME？），请人工确认与 OSS_REGION=$oss_region 一致"
    elif [ "$ep_short" != "$region_short" ]; then
      fail "OSS 地域一致性" "endpoint 地域 '$ep_region' 与 OSS_REGION '$oss_region' 不一致 —— 签名会失败"
    else
      pass "OSS 地域一致性" "${oss_region}（与 endpoint 一致）"
    fi

    # 内网端点必须与**机器**同地域。
    # 判据用云元数据（100.100.100.200/latest/meta-data/region-id）——
    # ⚠️ 不要用「IP 归属地」判断：阿里云公网 IP 段按注册地分配，
    #    查 IP 会得到「杭州」，而机器实际在 cn-wuhan-lr，结论完全相反。
    machine_region=""
    if command -v curl >/dev/null 2>&1; then
      machine_region="$(curl -s -m 3 http://100.100.100.200/latest/meta-data/region-id 2>/dev/null | tr -d '\r\n' | head -c 64)"
    fi
    case "$oss_endpoint" in
      *-internal.*)
        if [ -n "$machine_region" ]; then
          if [ "$machine_region" = "$ep_short" ]; then
            pass "OSS 内网端点" "机器地域 ${machine_region} = bucket 地域 ${ep_short}，内网端点可用（流量费 0）"
          else
            fail "OSS 内网端点" "机器地域 ${machine_region} ≠ bucket 地域 ${ep_short} —— -internal 端点用不了；改用公网端点 oss-<region>.aliyuncs.com（会有出方向流量费）"
          fi
        else
          warn "OSS 内网端点" "读不到本机地域元数据，无法自动判定是否同地域 —— 请人工确认轻量服务器与 bucket 同地域，否则 -internal 端点不可用"
        fi
        ;;
      *)
        pass "OSS 内网端点" "使用公网端点（可用，但出方向流量按公网计费）"
        ;;
    esac
  fi
elif [ "$storage" = "local" ]; then
  if [ "$(v STASHBOX_ALLOW_LOCAL_STORAGE)" = "1" ]; then
    # local 模式是**内测期推荐路径**：省掉一个要开通、要计费、
    # 还要盯流量账单的外部依赖（OSS）。轻量 200Mbps 不限月流量，
    # 音频走自己的 nginx 就行（见 deploy/README.md 的 9.4-b）。
    warn "STORAGE_PROVIDER" "local（已显式确认 STASHBOX_ALLOW_LOCAL_STORAGE=1）—— 音频落本地盘，多副本会互相看不到；磁盘满需要清理策略"
  else
    fail "STORAGE_PROVIDER" "local —— 生产必须 oss。确实要用本地盘，显式写 STASHBOX_ALLOW_LOCAL_STORAGE=1 确认"
  fi
  # local 模式的三件套：目录能写 + 对外 URL 可达 + 磁盘还剩多少
  local_dir="$(v LOCAL_AUDIO_DIR)"
  if [ -z "$local_dir" ]; then
    fail "LOCAL_AUDIO_DIR" "未设置 —— local 模式必须指定音频目录（默认 /tmp/audio 会被清空，不能用）"
  elif [ ! -d "$local_dir" ]; then
    fail "LOCAL_AUDIO_DIR" "$local_dir 不存在 —— 手工 mkdir 并 chown stashbox:stashbox"
  elif [ ! -w "$local_dir" ]; then
    fail "LOCAL_AUDIO_DIR" "$local_dir 不可写（服务用户 stashbox 写不进去）"
  else
    pass "LOCAL_AUDIO_DIR" "$local_dir（存在且可写）"
  fi
  local_url="$(v LOCAL_STORAGE_PUBLIC_URL)"
  if [ -z "$local_url" ]; then
    fail "LOCAL_STORAGE_PUBLIC_URL" "未设置 —— 客户端拿不到音频 URL（代码会兜底成 PUBLIC_GATEWAY_URL + /audio，但要显式写死域名）"
  else
    pass "LOCAL_STORAGE_PUBLIC_URL" "$local_url"
  fi
  # 磁盘余量：音频会一直涨，满了就写不进去（而且失败得很隐蔽）
  if command -v df >/dev/null 2>&1 && [ -d "$local_dir" ]; then
    avail_mb="$(df -Pm "$local_dir" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ -n "$avail_mb" ]; then
      if [ "$avail_mb" -lt 2048 ]; then
        fail "音频磁盘余量" "只剩 ${avail_mb} MB —— 音频会写不进去。加清理策略（删 N 天前的文件）或换 STORAGE_PROVIDER=oss"
      elif [ "$avail_mb" -lt 5120 ]; then
        warn "音频磁盘余量" "剩 ${avail_mb} MB —— 内测量级够，但上量前要加清理策略"
      else
        pass "音频磁盘余量" "${avail_mb} MB 可用"
      fi
    fi
  fi
else
  fail "STORAGE_PROVIDER" "值非法：'$storage'（只允许 oss / local）"
fi

# ═════════════════════════════════════════════════════════════════════
# 8. 推送通道
# ═════════════════════════════════════════════════════════════════════
push_url="$(v STASHBOX_PUSH_WEBHOOK_URL)"
if [ -z "$push_url" ]; then
  if [ "$(v STASHBOX_ALLOW_NO_PUSH)" = "1" ]; then
    warn "STASHBOX_PUSH_WEBHOOK_URL" "未配置（已显式确认 STASHBOX_ALLOW_NO_PUSH=1）—— 通知会标 failed，不会真的发出去"
  else
    fail "STASHBOX_PUSH_WEBHOOK_URL" "未配置 —— 推送会全部失败。要么配厂商网关，要么显式写 STASHBOX_ALLOW_NO_PUSH=1 确认不做推送"
  fi
else
  pass "STASHBOX_PUSH_WEBHOOK_URL" "$push_url"
fi

# ═════════════════════════════════════════════════════════════════════
# 9. 应用内更新（安卓端版本提示）
# ═════════════════════════════════════════════════════════════════════
app_code="$(v STASHBOX_APP_LATEST_VERSION_CODE)"
app_name="$(v STASHBOX_APP_LATEST_VERSION_NAME)"
app_dl="$(v STASHBOX_APP_DOWNLOAD_URL)"
if [ -z "$app_code" ] || [ "$app_code" = "0" ]; then
  if [ "$(v STASHBOX_ALLOW_NO_APP_VERSION)" = "1" ]; then
    warn "STASHBOX_APP_LATEST_VERSION_CODE" "未设置（已显式确认 STASHBOX_ALLOW_NO_APP_VERSION=1）—— 内测不做版本提示"
  else
    fail "STASHBOX_APP_LATEST_VERSION_CODE" "未设置 —— 内测期客户端拿不到版本信息。要么配版本号，要么显式写 STASHBOX_ALLOW_NO_APP_VERSION=1"
  fi
else
  pass "STASHBOX_APP_LATEST_VERSION_CODE" "$app_code"
  if [ -z "$app_name" ]; then
    warn "STASHBOX_APP_LATEST_VERSION_NAME" "未设置 —— 客户端会显示空版本名"
  else
    pass "STASHBOX_APP_LATEST_VERSION_NAME" "$app_name"
  fi
  if [ -z "$app_dl" ]; then
    fail "STASHBOX_APP_DOWNLOAD_URL" "未设置 —— 但版本号已置为 ${app_code}，客户端会提示更新却没地方下载"
  else
    pass "STASHBOX_APP_DOWNLOAD_URL" "$app_dl"
  fi
fi

# ═════════════════════════════════════════════════════════════════════
# 10. LLM / TTS provider
# ═════════════════════════════════════════════════════════════════════
llm="$(v LLM_PROVIDER)"; llm="${llm:-openai}"
if [ "$llm" = "mock" ]; then
  fail "LLM_PROVIDER" "mock —— 生产不会真的生成内容"
else
  pass "LLM_PROVIDER" "$llm"
fi

# LLM 的 key 必须真的有值：mock provider 已被代码删除（factory 只认 openai/qwen_vl），
# 所以 key 为空不会在启动时报错，而是等到第一条蒸馏任务才 401 —— 内测时才发现就晚了。
#
# ⚠️ key 有**两个来源**，任一有值就算通过：
#   · env（OPENAI_LLM_API_KEY / DASHSCOPE_API_KEY）
#   · **管理后台配的**（system_config 的 llm 条目，存在 DB 里）
#     运行时以 DB > env 生效（ai-service/llm/factory.py:reload()），
#     蒸馏任务启动前会自动 reload，所以后台改完**立即生效，不用重启**。
#     只看 env 会误报「key 没配」，而实际上后台已经配好了 —— 这就是
#     「功能不完善」的真实表现。
#
# 这里实际去查一次 DB（不是靠猜）：能连上 PG 就查，查不到就退回只看 env。
db_llm_key=""
if command -v psql >/dev/null 2>&1; then
  pg_pw="$(v POSTGRES_PASSWORD)"
  pg_u="$(v POSTGRES_USER)"
  pg_d="$(v POSTGRES_DB)"
  pg_h="$(v POSTGRES_HOST)"
  if [ -n "$pg_pw" ] && [ -n "$pg_u" ] && [ -n "$pg_d" ]; then
    db_llm_key="$(
      PGPASSWORD="$pg_pw" psql -h "${pg_h:-127.0.0.1}" -U "$pg_u" -d "$pg_d" -tAc \
        "select coalesce((value->>'api_key'),'') from system_config where key='llm';" 2>/dev/null \
      | tr -d '\r\n' | head -c 4
    )"
  fi
fi

case "$llm" in
  qwen*|dashscope*)
    env_key="$(v DASHSCOPE_API_KEY)$(v QWEN_VL_API_KEY)"
    ;;
  *)
    env_key="$(v OPENAI_LLM_API_KEY)"
    ;;
esac

if [ -n "$env_key" ]; then
  pass "LLM API Key" "env 里已设置（${llm}）"
elif [ -n "$db_llm_key" ]; then
  pass "LLM API Key" "管理后台已配置（${llm}，DB 覆盖 env，立即生效）"
else
  fail "LLM API Key" "env 与管理后台都没有 key（${llm}）—— 蒸馏会在运行时才 401。要么写 OPENAI_LLM_API_KEY / DASHSCOPE_API_KEY，要么在管理后台「LLM 设置」里配"
fi

tts="$(v TTS_PROVIDER)"; tts="${tts:-openai}"
if [ "$tts" = "mock" ]; then
  fail "TTS_PROVIDER" "mock —— 生产不产音频"
elif [ "$tts" = "local" ]; then
  fail "TTS_PROVIDER" "local —— 走 macOS 的 say，Linux 容器里不可用"
elif [ "$tts" = "indextts" ]; then
  warn "TTS_PROVIDER" "indextts —— 自建服务 infra/mlx-audio-tts-service 是 macOS 专属，容器里必须另部署一个 TTS 端点并改 INDEXTTS_BASE_URL"
else
  pass "TTS_PROVIDER" "$tts"
  # 同 LLM：edge / openai / doubao 等需要 key 的 provider，
  # 也能在**管理后台**配（system_config 的 tts 条目，运行时 DB > env）。
  # 只看 env 会误报 —— 后台配好了却报「没配」，这正是「功能不完善」的表现。
  case "$tts" in
    edge) : ;;   # edge 免费无 key，不需要
    *)
      tts_env_key="$(v OPENAI_TTS_API_KEY)$(v DOUBAO_TTS_API_KEY)$(v DOUBAO_TTS_TOKEN)$(v VOLCENGINE_TTS_API_KEY)"
      db_tts_key=""
      if command -v psql >/dev/null 2>&1 && [ -n "${db_llm_key:-}" ]; then
        db_tts_key="$(
          PGPASSWORD="$pg_pw" psql -h "${pg_h:-127.0.0.1}" -U "$pg_u" -d "$pg_d" -tAc \
            "select coalesce((value->>'api_key'),'') from system_config where key='tts';" 2>/dev/null \
          | tr -d '\r\n' | head -c 4
        )"
      fi
      if [ -n "$tts_env_key" ]; then
        pass "TTS API Key" "env 里已设置（${tts}）"
      elif [ -n "$db_tts_key" ]; then
        pass "TTS API Key" "管理后台已配置（${tts}，DB 覆盖 env，立即生效）"
      else
        fail "TTS API Key" "env 与管理后台都没有 key（${tts}）—— 合成会失败。要么写对应 env，要么在管理后台「TTS 设置」里配"
      fi
      ;;
  esac
fi

# ═════════════════════════════════════════════════════════════════════
# 11. 网关 → 下游接线（端口对不上 = 全量 502）
# ═════════════════════════════════════════════════════════════════════
for kv in "USER_SERVICE_URL:$(v USER_SERVICE_URL)" \
          "CONTENT_SERVICE_URL:$(v CONTENT_SERVICE_URL)" \
          "AI_SERVICE_URL:$(v AI_SERVICE_URL)"; do
  key="${kv%%:*}"; val="${kv#*:}"
  if [ -z "$val" ]; then
    skip "$key" "未设置 —— compose 会接管（容器服务名 + 810x）"
  elif printf '%s' "$val" | grep -qE ':(8001|8002|8003)(/|$)'; then
    fail "$key" "指向旧端口（${val}）—— 容器里监听的是 8101/8102/8103，会全量 502"
  else
    pass "$key" "$val"
  fi
done

# ═════════════════════════════════════════════════════════════════════
# 12. ffmpeg（音频链路的硬依赖）
# ═════════════════════════════════════════════════════════════════════
ffmpeg_bin="$(v FFMPEG_BIN)"; ffmpeg_bin="${ffmpeg_bin:-/usr/bin/ffmpeg}"
if have ffmpeg; then
  pass "ffmpeg" "本机 $(command -v ffmpeg)"
elif [ -x "$ffmpeg_bin" ]; then
  pass "ffmpeg" "$ffmpeg_bin"
elif have docker; then
  if docker image inspect stashbox-backend:ai-service >/dev/null 2>&1; then
    if docker run --rm --entrypoint ffmpeg stashbox-backend:ai-service -version >/dev/null 2>&1; then
      pass "ffmpeg" "镜像内存在（stashbox-backend:ai-service）"
    else
      fail "ffmpeg" "镜像 stashbox-backend:ai-service 里没有 ffmpeg —— 蒸馏拼接与多码率转码会断"
    fi
  else
    warn "ffmpeg" "本机没有，镜像 stashbox-backend:ai-service 还没构建 —— 构建后重跑本检查"
  fi
else
  fail "ffmpeg" "本机没有 ffmpeg，也没有 docker 可查镜像"
fi

# ═════════════════════════════════════════════════════════════════════
# 13. 迁移状态（DB 里的 alembic_version vs 仓库 head）
# ═════════════════════════════════════════════════════════════════════
if [ "${SKIP_NETWORK:-0}" = "1" ]; then
  skip "迁移状态" "SKIP_NETWORK=1，已跳过"
elif [ -z "$pg_host" ]; then
  skip "迁移状态" "没有 POSTGRES_HOST，跳过"
elif ! have psql && ! have docker; then
  skip "迁移状态" "没有 psql / docker，无法查询数据库"
else
  db_rev=""
  if have psql; then
    db_rev="$(PGPASSWORD="$pg_pass" psql -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" \
                 -tAc "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null | tr -d '[:space:]')"
  else
    db_rev="$(docker run --rm --network bridge \
                -e PGPASSWORD="$pg_pass" \
                postgres:15-alpine \
                psql "postgresql://$pg_user@$pg_host:$pg_port/$pg_db" -tAc \
                "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null | tr -d '[:space:]')"
  fi
  head_rev="$(alembic_head "$BACKEND_DIR/alembic/versions")"
  if [ -z "$db_rev" ]; then
    fail "迁移状态" "数据库里没有 alembic_version —— 迁移没跑过（compose 的 migrate 服务会跑）"
  elif [ -z "$head_rev" ]; then
    warn "迁移状态" "数据库版本 ${db_rev}（算不出仓库 head，无法比对）"
  elif [ "$db_rev" = "$head_rev" ]; then
    pass "迁移状态" "数据库已在 head（${db_rev}）"
  else
    fail "迁移状态" "数据库在 ${db_rev}，仓库 head 是 $head_rev —— 必须先跑迁移（compose 的 migrate 服务）"
  fi
fi

# ═════════════════════════════════════════════════════════════════════
# 14. compose 文件能解析
# ═════════════════════════════════════════════════════════════════════
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.production.yml"
if have docker && docker compose version >/dev/null 2>&1; then
  if docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config >/dev/null 2>&1; then
    pass "compose 解析" "docker-compose.production.yml OK"
  else
    fail "compose 解析" "docker-compose.production.yml 解析失败（看上面的报错）"
  fi
else
  skip "compose 解析" "本机没有 docker compose，跳过"
fi

# ═════════════════════════════════════════════════════════════════════
# 15. 客户端 IP 的两层信任边界必须配对
# ═════════════════════════════════════════════════════════════════════
# uvicorn 的 ProxyHeadersMiddleware（--proxy-headers 默认开）在 **ASGI 层**就
# 把 scope["client"] 改写成 X-Forwarded-For 里的地址 —— 应用代码看到请求
# 之前就已经改了。应用层（backend/common/ip.py）又独立判一次：peer 不在
# TRUSTED_PROXY_IPS 里就忽略 XFF，只认真 peer。
#
# 这两个是同一个决定的两个实现：
#   · 不一致 → 网关和下游对"这个 IP 是谁"的理解不同，限流按 A 计数、
#     审计按 B 记，永远对不上；
#   · 代理在另一台机器上时，所有人算一个 IP → 一个人就能把其他人全锁在门外。
fai="$(v FORWARDED_ALLOW_IPS)"
if [ -z "$fai" ]; then
  warn "FORWARDED_ALLOW_IPS" "未设置 —— uvicorn 会退回它自己的默认 127.0.0.1,::1。compose 路径不受影响（它用 --forwarded-allow-ips 传 TRUSTED_PROXY_IPS），但 **systemd 部署路径必须显式设置**并与 TRUSTED_PROXY_IPS 一致"
elif [ "$fai" = "*" ]; then
  fail "FORWARDED_ALLOW_IPS" "是 '*' —— 公网客户端可伪造 X-Forwarded-For 绕过登录限流"
else
  pass "FORWARDED_ALLOW_IPS" "$fai"
fi

if [ -n "$fai" ] && [ -n "$proxy_ips" ] && [ "$fai" != "$proxy_ips" ]; then
  fail "代理白名单一致性" "FORWARDED_ALLOW_IPS='$fai' 与 TRUSTED_PROXY_IPS='$proxy_ips' 不一致 —— ASGI 层和应用层对'这个 IP 是谁'的理解不同，登录限流和审计永远对不上。两个值必须**完全相同**"
elif [ -n "$fai" ] && [ -n "$proxy_ips" ] && [ "$fai" != "*" ] && [ "$proxy_ips" != "*" ]; then
  pass "代理白名单一致性" "FORWARDED_ALLOW_IPS 与 TRUSTED_PROXY_IPS 一致（${proxy_ips}）"
else
  skip "代理白名单一致性" "有一边没配、或填了 '*'，见上面几条"
fi

# ═════════════════════════════════════════════════════════════════════
# 16. 主机资源（2 GiB 轻量应用服务器）
# ═════════════════════════════════════════════════════════════════════
# 这条部署路径要求一台**确实有 2 GiB** 的机器：5 个 Python 进程 + PostgreSQL
# + Redis + nginx + OS 同时在上面。内存不够时不会立刻报错，而是变成
# "服务随机重启 / 机器卡死" —— 预检阶段拦住它比半夜排障便宜。
mem_total_kb=""
# 正常就是 /proc/meminfo；STASHBOX_MEMINFO_FILE 只是给"在非 Linux 机器上
# 验证这段判断"用的（macOS 上没有 /proc/meminfo）。
meminfo_file="${STASHBOX_MEMINFO_FILE:-/proc/meminfo}"
if [ -r "$meminfo_file" ]; then
  mem_total_kb="$(awk '/^MemTotal:/ {print $2; exit}' "$meminfo_file" 2>/dev/null | tr -d '[:space:]')"
fi
case "$mem_total_kb" in
  ''|*[!0-9]*) mem_total_kb=0 ;;
esac
if [ "$mem_total_kb" -eq 0 ]; then
  skip "主机内存" "读不到 /proc/meminfo（不是 Linux 主机？）—— 跳过"
else
  mem_total_mb=$((mem_total_kb / 1024))
  if [ "$mem_total_mb" -lt 1536 ]; then
    fail "主机内存" "只有 ${mem_total_mb} MB（< 1536 MB）—— 这个部署路径要求 >= 2 GiB（5 个 Python 进程 + PG + Redis + nginx 同机），低于 1.5 GiB 一定跑不起来"
  elif [ "$mem_total_mb" -lt 2048 ]; then
    warn "主机内存" "只有 ${mem_total_mb} MB（< 2048 MB）—— 低于 2 GiB 规格，swap 会频繁介入，服务会明显变慢"
  else
    pass "主机内存" "${mem_total_mb} MB（>= 2048 MB）"
  fi
fi

# Docker 守护进程在这台机器上 = 空载 150-200 MB + overlay2 存第二份层。
# 2 GiB 的生产路径是 systemd，不用 Docker。
if have docker && docker info >/dev/null 2>&1; then
  warn "Docker 守护进程" "本机 dockerd 正在运行（空载 150-200 MB）—— 2 GiB 机器的生产路径应该是 systemd（deploy/systemd/），确认没有在用 compose 起这套栈"
else
  pass "Docker 守护进程" "未运行（符合 2 GiB 机器的 systemd 部署路径）"
fi

# ═════════════════════════════════════════════════════════════════════
# 17. 本地可观测性栈：**只用于本地开发**，不能在这台机器上跑
# ═════════════════════════════════════════════════════════════════════
# prometheus + grafana + alertmanager + blackbox_exporter + redis_exporter
# 合计约 400-500 MB —— 2 GiB 机器的四分之一，只为了给一个"一天看一次图"的人。
# 生产监控走阿里云 云监控 / SLS（见 .env.production.2g.example 的注释）。
obs_running=""
if have docker && docker info >/dev/null 2>&1; then
  obs_running="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null \
    | grep -Ei 'prometheus|grafana|alertmanager|blackbox|redis[_-]?exporter' || true)"
fi
if [ -n "$obs_running" ]; then
  fail "本地可观测性栈" "本机正在跑 Prometheus/Grafana/Alertmanager/exporter —— 这套东西约 400-500 MB，2 GiB 机器放不下（会直接把 PG 和 Python 服务挤死）。生产请用阿里云云监控 / SLS"
else
  pass "本地可观测性栈" "本机没在跑 Prometheus/Grafana（生产用阿里云云监控 / SLS）"
fi

obs_compose="$BACKEND_DIR/../infra/docker/docker-compose.observability.yml"
if [ -f "$obs_compose" ]; then
  pass "可观测性编排文件" "infra/docker/docker-compose.observability.yml 存在 —— 它是**本地开发**用的，不要在生产机器上起"
fi

# ═════════════════════════════════════════════════════════════════════
# 2 GiB 机器的关键配置（漏配的表现是「服务正常但很慢」）
# ═════════════════════════════════════════════════════════════════════
#
# ⚠️ 为什么要单独查这几项：它们**缺失时不会报错**，只是行为退化，
#    而表现极具迷惑性（服务健康检查全绿、功能"能跑"但慢到超时）。
#    实际踩过的坑：手工写 env 时漏了 STASHBOX_FETCH_BROWSER_TIER=0，
#    于是每次剪藏都升级到浏览器通道 → 浏览器二进制没装 →
#    白等 30 秒抓取预算 → 45 秒超时、剪藏失败、文章不入库。

# 浏览器抓取通道：2 GiB 机器上必须关（chromium 300MB+ 是 content-service 的子进程）
browser_tier="$(v STASHBOX_FETCH_BROWSER_TIER)"
if [ -z "$browser_tier" ]; then
  fail "STASHBOX_FETCH_BROWSER_TIER" "未设置 —— 默认**开**！本机没装 playwright 二进制，剪藏会白等 30 秒抓取预算后超时。必须写 0"
elif [ "$browser_tier" = "1" ] || [ "$browser_tier" = "true" ]; then
  if [ -d "${HOME:-/root}/.cache/ms-playwright" ] || [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    warn "STASHBOX_FETCH_BROWSER_TIER" "开了浏览器通道 —— 确认这台机器的 content-service MemoryMax（176M）容得下 chromium（RSS 300MB+）"
  else
    fail "STASHBOX_FETCH_BROWSER_TIER" "开了但没装 playwright 二进制 —— 剪藏会白等 30 秒后超时。要么装（playwright install chromium），要么写 0"
  fi
else
  pass "STASHBOX_FETCH_BROWSER_TIER" "$browser_tier（已关闭，不会在浏览器通道上白等）"
fi

# Arq 并发：2 GiB 机器上蒸馏并发必须压低（每个并发都要内存 + ffmpeg 子进程）
arq_jobs="$(v ARQ_MAX_JOBS)"
if [ -z "$arq_jobs" ]; then
  fail "ARQ_MAX_JOBS" "未设置 —— 代码默认值对 2 GiB 机器偏高，蒸馏并发会挤爆内存"
elif [ "$arq_jobs" -gt 4 ] 2>/dev/null; then
  fail "ARQ_MAX_JOBS" "$arq_jobs —— 2 GiB 机器上偏高，建议 ≤ 4（默认 2）"
else
  pass "ARQ_MAX_JOBS" "$arq_jobs"
fi

# MCP 工具：会拉起额外的模型/网络调用
mcp_tools="$(v ENABLE_MCP_TOOLS)"
if [ "$mcp_tools" = "1" ] || [ "$mcp_tools" = "true" ]; then
  warn "ENABLE_MCP_TOOLS" "开了 —— 会额外拉起 MCP 工具链，确认内存与出网没问题"
else
  pass "ENABLE_MCP_TOOLS" "${mcp_tools:-0}（关闭）"
fi

# TTS 测试端点：会真的调 LLM/TTS 烧钱
llm_test="$(v ENABLE_LLM_TEST_ENDPOINT)"
if [ "$llm_test" = "1" ] || [ "$llm_test" = "true" ]; then
  warn "ENABLE_LLM_TEST_ENDPOINT" "开了 —— 任何人调这个端点都会真的花钱（LLM + TTS）"
else
  pass "ENABLE_LLM_TEST_ENDPOINT" "${llm_test:-0}（关闭）"
fi

# ═════════════════════════════════════════════════════════════════════
# 输出
# ═════════════════════════════════════════════════════════════════════
echo
echo "════════════════════════════════════════════════════════════════════"
printf '  预检结果   PASS %d   WARN %d   FAIL %d\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
echo "════════════════════════════════════════════════════════════════════"
for line in "${LINES[@]}"; do
  status="${line%%|*}"
  rest="${line#*|}"
  check="${rest%%|*}"
  detail="${rest#*|}"
  case "$status" in
    PASS) printf '  \033[32mPASS\033[0m  %-38s %s\n' "$check" "$detail" ;;
    WARN) printf '  \033[33mWARN\033[0m  %-38s %s\n' "$check" "$detail" ;;
    FAIL) printf '  \033[31mFAIL\033[0m  %-38s %s\n' "$check" "$detail" ;;
    *)    printf '  \033[90mSKIP\033[0m  %-38s %s\n' "$check" "$detail" ;;
  esac
done
echo "════════════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
  printf '\n\033[31m✗ 预检未通过：%d 项 FAIL —— 不要发版。\033[0m\n' "$FAIL_COUNT"
  exit 1
fi
if [ "$WARN_COUNT" -gt 0 ]; then
  printf '\n\033[33m△ 预检通过（有 %d 项 WARN，确认过再发）。\033[0m\n' "$WARN_COUNT"
else
  printf '\n\033[32m✓ 预检全部通过。\033[0m\n'
fi
exit 0
