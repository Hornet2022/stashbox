#!/usr/bin/env bash
# 巡检「当前版本」这件事的三个真相源是否一致。
#
# ─────────────────────────────────────────────────────────────────
# 为什么需要这个脚本
#
# 「当前版本」这件事有**三处**记录：
#   ① 服务端 env 的 5 个 STASHBOX_APP_* 变量（版本接口读它）
#   ② /downloads/latest.apk 软链（营销页下载入口指向它）
#   ③ 实际文件 /var/www/stashbox-downloads/tingxia-<ver>.apk
# 三处各说各的时，**没有任何一处会报错** —— 页面上看起来一切正常。
#
# 2026-11-09 实测踩的就是这个：手工分三步发版（scp → 软链 → env），
# **漏了 env 那一步** ⇒
#   · 服务器上有 0.5.2 的包，软链也指向它（③ ✓ ② ✓）
#   · 但 env 还停在 0.5.1 ⇒ 版本接口返回 code=24（① ✗）
#   · 安卓客户端判 `latest > installed` 为假 ⇒ 显示"已是最新版本"
#   · ⇒ **根本没有下载入口** ⇒ 内测用户反馈"无法下载新版本"
#
# 症状离原因隔了三层（包在 → 软链对 → 但版本接口说旧号），
# 排查时很容易怀疑"下载挂了""包坏了""网络问题" —— 全都不是。
#
# 所以要有一个**独立于发版流程**的巡检：
# 发版脚本（release_apk.sh）自己校验自己写的东西，天然看不见
# "有人绕过脚本手工发版"这种情况。本脚本站在外面看，**什么时候跑都行**。
#
# ─────────────────────────────────────────────────────────────────
# 用法
#
#   ./verify_release_consistency.sh          # 巡检（默认只读，不改任何东西）
#
# 退出码：0 = 三处一致；1 = 不一致（每处不一致都会点名是谁）
set -uo pipefail

HOST="${STASHBOX_HOST:-8.148.9.151}"
SSH_KEY="${STASHBOX_SSH_KEY:-$HOME/.ssh/stashbox_deploy}"
REMOTE_DIR="${STASHBOX_REMOTE_DIR:-/var/www/stashbox-downloads}"
ENV_FILE="${STASHBOX_ENV_FILE:-/etc/stashbox/stashbox.env}"
VERSION_API="${STASHBOX_VERSION_API:-https://api.stashbox.cn/api/v1/app/version}"
DOWNLOAD_BASE="${STASHBOX_DOWNLOAD_BASE:-https://stashbox.cn/downloads}"

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "root@${HOST}")

echo "▶ 读取三个真相源"

# ── 真相源 ①：远端 env 的 5 个变量 ───────────────────────────────────
# 只取需要的键，避免把整个 env（含密钥）拉回本地。
ENV_BLOCK="$("${SSH[@]}" "grep -E '^STASHBOX_APP_(DOWNLOAD_URL|LATEST_VERSION_CODE|LATEST_VERSION_NAME|PUBLISHED_AT|RELEASE_NOTES)=' '${ENV_FILE}'" | tr -d '\r')"

# ── 真相源 ②③：软链指向 + 实际文件 ─────────────────────────────────
REMOTE_BLOCK="$("${SSH[@]}" "
  cd '${REMOTE_DIR}' || exit 1
  printf 'LINK=%s\n' \"\$(readlink -f latest.apk 2>/dev/null | xargs -r basename)\"
  # ⚠️ 必须用 stat **-L**（跟随软链）：不带 -L 时 stat 量的是**链接名本身的长度**
  # （实测 17 字节 = \"latest.apk\" 这个名字），不是包的大小 ——
  # 那是个会误导排查的假读数（看着像"包坏了/包空了"）。
  printf 'FILE_EXISTS=%s\n' \"\$([ -f latest.apk ] && echo yes || echo no)\"
  printf 'FILE_SIZE=%s\n' \"\$(stat -L -c %s latest.apk 2>/dev/null || echo 0)\"
" | tr -d '\r')"

# ── 真相源 ①的对外表现：版本接口 ─────────────────────────────────────
API_JSON="$(curl -s --max-time 20 "$VERSION_API")"

# ── 对拍 ────────────────────────────────────────────────────────────
# 判据在 deploy/release_consistency.py 里（**纯函数，可测**）——
# 内嵌在这里的话就没法被测试直接覆盖 ⇒ 每条判据都得靠"人工跑一次看它报不报"
# 来验证 ⇒ 护栏没有牙齿。抽成模块后，脚本与测试共用**同一份**判定实现。
#
# 用法：ENV_BLOCK / REMOTE_BLOCK / API_JSON 三个环境变量喂进去。
ENV_BLOCK="$ENV_BLOCK" REMOTE_BLOCK="$REMOTE_BLOCK" API_JSON="$API_JSON" \
python3 "$(dirname "$0")/release_consistency.py"
rc=$?

# ── 外网可达性（版本接口说的那个地址，用户真的点得到吗）─────────────
if [[ $rc -eq 0 ]]; then
  echo
  echo "▶ 外网可达性"
  HTTP="$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 60 "${DOWNLOAD_BASE}/latest.apk")"
  TYPE="$(curl -s -o /dev/null -w '%{content_type}' -L --max-time 60 "${DOWNLOAD_BASE}/latest.apk")"
  echo "  ${DOWNLOAD_BASE}/latest.apk → HTTP ${HTTP} / ${TYPE}"
  # ⚠️ 状态码 200 不等于拿到的是 APK —— /downloads/ 路由漏掉时 try_files
  # 会返回 index.html，**状态码还是 200**（这个坑踩过两次）。所以要看 MIME。
  if [[ "$HTTP" != "200" ]]; then
    echo "✗ 下载入口返回 ${HTTP}" >&2
    exit 1
  fi
  if [[ "$TYPE" != "application/vnd.android.package-archive" ]]; then
    echo "✗ 下载到的 MIME 是 ${TYPE}，不是 APK（多半是 SPA 的 index.html）" >&2
    exit 1
  fi
  echo "  ✓ 拿到的是真 APK"
fi

exit $rc
