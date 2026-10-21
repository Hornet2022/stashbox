#!/usr/bin/env bash
#
# 首次 SSH 接入 + 部署密钥安装
# =============================
#
# 用途：轻量服务器只能用「密码」或「控制台绑定密钥」首次登录。
# 这个脚本用密码登录一次，然后把本机的部署公钥写进
# /root/.ssh/authorized_keys —— 之后所有操作走密钥，不再需要密码。
#
# 用法：
#     ./ssh-init.sh <root密码>
#
# 幂等：重复跑不会重复追加同一把公钥。
#
# ⚠️ 密码会出现在 shell 历史里。跑完记得清：
#     history -d <行号>  或  history -c

set -uo pipefail

HOST="${STASHBOX_HOST:-8.148.9.151}"
USER="${STASHBOX_USER:-root}"
KEY="${STASHBOX_KEY:-$HOME/.ssh/stashbox_deploy}"
PUB="${KEY}.pub"
PORT="${STASHBOX_PORT:-22}"

if [ $# -lt 1 ]; then
  printf '用法：%s <root密码>\n' "$0" >&2
  exit 2
fi
PASSWORD="$1"

[ -f "$PUB" ] || { printf '✗ 找不到公钥：%s\n' "$PUB" >&2; exit 1; }

printf '▶ 测试 %s@%s:%s 可达性…\n' "$USER" "$HOST" "$PORT"
if ! nc -z -w 8 "$HOST" "$PORT" 2>/dev/null; then
  printf '✗ %s:%s 不可达 —— 检查轻量控制台的防火墙是否放行了 22 端口\n' "$HOST" "$PORT" >&2
  exit 1
fi

printf '▶ 用密码登录并安装公钥…\n'
# 公钥内容通过 stdin 传（不拼进命令行，避免出现在 ps 输出里）
PUBKEY_CONTENT="$(cat "$PUB")"

# 用 -o 严格选项挡住「首次连接不知道主机密钥」的交互提问 ——
# known_hosts 里已经有这台机器的密钥（之前 ssh-keyscan 写过并校验过指纹）。
sshpass -p "$PASSWORD" ssh \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
  -o ConnectTimeout=15 \
  -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no \
  "${USER}@${HOST}" \
  "set -e
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
   if grep -qF '${PUBKEY_CONTENT}' ~/.ssh/authorized_keys; then
     echo 'KEY_ALREADY_PRESENT'
   else
     echo '${PUBKEY_CONTENT}' >> ~/.ssh/authorized_keys
     echo 'KEY_ADDED'
   fi
   chmod 600 ~/.ssh/authorized_keys
   echo '--- 机器信息 ---'
   hostname
   . /etc/os-release 2>/dev/null && echo \"\$PRETTY_NAME\"
   nproc | sed 's/^/CPU 核数: /'
   free -m | awk '/^Mem:/{printf \"内存: %s MB 总 / %s MB 空闲\n\", \$2, \$4}'
   df -h / | awk 'NR==2{print \"系统盘: \" \$2 \" 总 / \" \$4 \" 已用 / \" \$5}'
   uname -r | sed 's/^/内核: /'
  " 2>&1

rc=$?
if [ $rc -ne 0 ]; then
  printf '\n✗ 密码登录失败（rc=%s）。请检查密码是否正确。\n' "$rc" >&2
  exit $rc
fi

printf '\n▶ 验证密钥登录…\n'
if ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes \
     "${USER}@${HOST}" 'echo "KEY_LOGIN_OK: $(hostname) / $(id -un)"' 2>&1; then
  printf '\n✓ 密钥登录成功。以后用这个命令连：\n'
  printf '    ssh -i %s %s@%s\n' "$KEY" "$USER" "$HOST"
  printf '\n⚠️ 记得清掉 shell 历史里的密码：history -c\n'
else
  printf '\n✗ 密钥登录失败。密码可能没输对，或 authorized_keys 权限不对。\n' >&2
  exit 1
fi
