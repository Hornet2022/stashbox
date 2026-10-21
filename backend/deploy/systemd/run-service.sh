#!/usr/bin/env bash
# 统一启动脚本（systemd 用）
# ========================
#
# 为什么要这个脚本，而不是把整条 uvicorn 命令写进 ExecStart？
#
#   1. systemd 的 ExecStart **不做 shell 变量展开**。Docker 那条路径用的是
#      `sh -c "... --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""`
#      （systemd 会把 `${VAR:-default}` 当成变量名 "VAR:-default" 展开成空串），
#      直接照抄到 ExecStart 里会得到 `--forwarded-allow-ips ""`，
#      真实客户端 IP 就丢了 → 登录限流退化成按网关 IP 计数 → 一个人能锁死所有人。
#      放进 bash 脚本里，`:-127.0.0.1` 的语义才是 shell 的语义。
#   2. 四个服务的启动命令只有服务名和端口不同，放一个脚本里就不会漂移。
#
# ⚠️ 关于 X-Forwarded-For 的两层信任判断（**这里刻意不传 --forwarded-allow-ips**）：
#
#   uvicorn 的 CLI 参数 `--forwarded-allow-ips` 默认是 None；为 None 时它会去读
#   **环境变量 FORWARDED_ALLOW_IPS**（再退回 "127.0.0.1,::1"）。
#   所以这里**不传**这个参数 —— 命令行传的值会**覆盖**环境变量，
#   env 文件里的 FORWARDED_ALLOW_IPS 就形同虚设，两层信任就可能不一致。
#
#   客户端 IP 有**两个独立的**信任边界，两处必须配成**同一个**代理列表：
#     · ASGI 层（uvicorn 的 ProxyHeadersMiddleware，--proxy-headers 默认开）：
#       peer 在 FORWARDED_ALLOW_IPS 里 → 在 ASGI 层就把 scope["client"]
#       改写成 XFF 里的地址 —— **应用代码看到请求之前就已经改了**。
#     · 应用层（backend/common/ip.py，读 settings.trusted_proxy_ips）：
#       peer 不在 TRUSTED_PROXY_IPS 里 → 忽略 XFF，只认真 peer。
#   两处不一致 → 网关和下游对"这个 IP 是谁"的理解不同 → 限流按 A 计数、
#   审计按 B 记；更糟的是代理在另一台机器时，所有人算一个 IP，
#   一个人就能把其他人全锁在门外。
#   → env 文件里两个变量都写，且必须完全一致（preflight.sh 会查）。
#
# 用法：run-service.sh <服务目录名> <端口>
#   run-service.sh api-gateway 8100
#   run-service.sh user-service 8101
#   run-service.sh content-service 8102
#   run-service.sh ai-service 8103
#
# 环境变量（都有默认值，可被 unit 的 Environment= / EnvironmentFile= 覆盖）：
#   STASHBOX_BACKEND_DIR   backend 目录（默认 /opt/stashbox/stashbox/backend）
#   STASHBOX_PYTHON_BIN    解释器（默认 /opt/stashbox/venv/bin/python）
#   STASHBOX_BIND_HOST     监听地址（默认 127.0.0.1 —— 公网入口只有 nginx）
#   FORWARDED_ALLOW_IPS    uvicorn 采信谁的 X-Forwarded-For
#                          （默认 127.0.0.1,::1 = uvicorn 自己的默认值）
#                          必须与 TRUSTED_PROXY_IPS 完全一致。

set -euo pipefail

service="${1:?用法: run-service.sh <服务目录名> <端口>}"
port="${2:?用法: run-service.sh <服务目录名> <端口>}"

backend_dir="${STASHBOX_BACKEND_DIR:-/opt/stashbox/stashbox/backend}"
python_bin="${STASHBOX_PYTHON_BIN:-/opt/stashbox/venv/bin/python}"
bind_host="${STASHBOX_BIND_HOST:-127.0.0.1}"

if [ ! -x "$python_bin" ]; then
  venv_bin="$(dirname "$python_bin")"
  printf '✗ 解释器不存在或不可执行：%s\n' "$python_bin" >&2
  printf '  先建 venv：python3 -m venv /opt/stashbox/venv && %s/pip install -U pip\n' "$venv_bin" >&2
  printf '  再装依赖（**用 poetry，不要用 requirements.txt**）：\n' >&2
  printf '    cd /opt/stashbox/stashbox/backend\n' >&2
  printf '    %s/pip install --no-cache-dir poetry==2.5.1\n' "$venv_bin" >&2
  printf '    %s/poetry install --only main --no-root\n' "$venv_bin" >&2
  exit 1
fi

# 关键运行时依赖自检：edge_tts（TTS）与 boto3（OSS）**不在**任何
# requirements.txt 里，只在 pyproject 里。装漏了不会在启动时报错，
# 而是等到第一条蒸馏任务（edge TTS）或第一次上传 OSS 才炸 ——
# 那时候用户已经在内测了。启动前拦住。
if ! "$python_bin" -c "import edge_tts, boto3" 2>/dev/null; then
  venv_bin="$(dirname "$python_bin")"
  printf '✗ 缺运行时依赖（edge_tts / boto3）—— 多半是用了 pip install -r requirements.txt\n' >&2
  printf '  正确装法：cd %s && %s/poetry install --only main --no-root\n' \
    "$backend_dir" "$venv_bin" >&2
  exit 1
fi

cd "${backend_dir}/${service}"

# --proxy-headers：让 uvicorn 处理 X-Forwarded-For / X-Forwarded-Proto。
#   （uvicorn 的默认值就是开，这里显式写出来是为了"看得见"。）
#
# **不传** --forwarded-allow-ips：让它读环境变量 FORWARDED_ALLOW_IPS
#   （由 unit 的 EnvironmentFile=/etc/stashbox/stashbox.env 提供）。
#   那必须与 TRUSTED_PROXY_IPS（应用层 common/ip.py 用的那个）**完全一致**。
#   ⚠️ 填 "*" = 任何公网客户端都能伪造来源 IP 绕过登录限流，别填。
exec "$python_bin" -m uvicorn main:app \
  --host "$bind_host" \
  --port "$port" \
  --proxy-headers
