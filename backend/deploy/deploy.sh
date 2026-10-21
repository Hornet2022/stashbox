#!/usr/bin/env bash
#
# Stashbox 生产部署脚本
# =====================
#
# 做的事：预检 → 构建镜像 → 跑迁移 → 起栈 → 等健康。
# 迁移作为 compose 里的独立一次性服务（migrate），用
# `condition: service_completed_successfully` 卡在所有服务之前。
#
# 用法：
#     ./deploy.sh                      # 生产（外部 RDS / Redis）
#     ./deploy.sh --local              # 本地跑通（叠加本地 PG / Redis 容器）
#     ./deploy.sh --skip-preflight     # 跳过预检（不建议；AKS 已有其它门禁时才用）
#     ./deploy.sh --build-only         # 只构建镜像
#     ./deploy.sh --migrate-only       # 只跑迁移
#     ./deploy.sh down                 # 停栈（不删数据卷）
#
# ⚠️ 本脚本**不会**把任何 secret 写进文件、打印到日志、或提交到 git。
#    它只从 deploy/.env.production 读配置（该文件已被 .gitignore 排除）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${STASHBOX_ENV_FILE:-$SCRIPT_DIR/.env.production}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.production.yml"
LOCAL_OVERRIDE="$SCRIPT_DIR/docker-compose.production.local.yml"

USE_LOCAL=0
SKIP_PREFLIGHT=0
BUILD_ONLY=0
MIGRATE_ONLY=0
ACTION="up"

for arg in "$@"; do
  case "$arg" in
    --local) USE_LOCAL=1 ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --build-only) BUILD_ONLY=1 ;;
    --migrate-only) MIGRATE_ONLY=1 ;;
    down) ACTION="down" ;;
    up) ACTION="up" ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) printf '未知参数：%s（用 --help 看用法）\n' "$arg" >&2; exit 2 ;;
  esac
done

# ── 前置检查 ──────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || {
  printf '✗ 没有 docker 命令。生产部署请在有 Docker 的机器上跑，或改用 ACK 部署。\n' >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  printf '✗ 没有 docker compose（v2 插件）。需要 Compose ≥ 2.24（env_file 的 required:false 从这版才有）。\n' >&2
  exit 1
}
[ -f "$ENV_FILE" ] || {
  printf '✗ 找不到 %s\n' "$ENV_FILE" >&2
  printf '  先做：cp %s/.env.production.example %s\n' "$SCRIPT_DIR" "$ENV_FILE" >&2
  exit 1
}

COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if [ "$USE_LOCAL" = "1" ]; then
  COMPOSE_ARGS+=(-f "$LOCAL_OVERRIDE")
fi

# ── 预检 ──────────────────────────────────────────────────────────────
if [ "$SKIP_PREFLIGHT" = "0" ] && [ "$ACTION" = "up" ]; then
  printf '▶ 预检…\n'
  if ! bash "$SCRIPT_DIR/preflight.sh" "$ENV_FILE"; then
    printf '\n✗ 预检未通过，已中止部署。\n' >&2
    exit 1
  fi
fi

case "$ACTION" in
  down)
    printf '▶ 停栈…\n'
    docker compose "${COMPOSE_ARGS[@]}" down
    exit $?
    ;;
esac

# ── 构建 ──────────────────────────────────────────────────────────────
if [ "$MIGRATE_ONLY" != "1" ]; then
  printf '▶ 构建镜像（4 个服务 + worker + migrate）…\n'
  # 一次 build，多个 target —— BuildKit 会复用公共层的缓存。
  if ! docker compose "${COMPOSE_ARGS[@]}" build; then
    printf '✗ 构建失败。\n' >&2
    exit 1
  fi
fi

if [ "$BUILD_ONLY" = "1" ]; then
  printf '✓ 构建完成（--build-only，未启动）。\n'
  exit 0
fi

# ── 迁移 ──────────────────────────────────────────────────────────────
# compose 里 migrate 是独立的一次性服务，up 时它会先跑完；
# 这里显式跑一次是为了：迁移失败时立刻看到报错，而不是让后面的服务莫名不启动。
printf '▶ 跑数据库迁移（alembic upgrade head）…\n'
if ! docker compose "${COMPOSE_ARGS[@]}" run --rm migrate; then
  printf '✗ 迁移失败 —— 服务没有启动。修完再发。\n' >&2
  exit 1
fi
printf '✓ 迁移完成。\n'

if [ "$MIGRATE_ONLY" = "1" ]; then
  printf '✓ 仅迁移完成（--migrate-only）。\n'
  exit 0
fi

# ── 启动 ──────────────────────────────────────────────────────────────
printf '▶ 启动服务…\n'
# compose 的 depends_on 已经保证：migrate 成功 → 3 个下游 healthy → 网关起。
if ! docker compose "${COMPOSE_ARGS[@]}" up -d --remove-orphans; then
  printf '✗ 启动失败。\n' >&2
  exit 1
fi

printf '\n▶ 状态：\n'
docker compose "${COMPOSE_ARGS[@]}" ps

GATEWAY_PORT="${GATEWAY_PORT:-8100}"
printf '\n✓ 部署完成。网关：\n'
printf '    本机：http://localhost:%s/healthz\n' "$GATEWAY_PORT"
printf '    健康：curl http://localhost:%s/readyz\n' "$GATEWAY_PORT"
printf '\n  日志：docker compose --env-file %s -f %s logs -f api-gateway\n' \
  "$ENV_FILE" "$COMPOSE_FILE"
if [ "$USE_LOCAL" = "1" ]; then
  printf '  （本地模式另加：-f %s）\n' "$LOCAL_OVERRIDE"
fi
