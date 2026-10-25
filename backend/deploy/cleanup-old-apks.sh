#!/usr/bin/env bash
# 清理对外可下载的历史 APK（保留最近 N 份）
#
# 为什么需要：
#   · /var/www/stashbox-downloads/ 用 nginx alias 静态直出 ⇒ **目录里有什么就能下载什么**。
#     内测期每次发版都往里放一个版本化文件 + 改 latest.apk 软链，
#     实测已积到 14 个（约 54MB），其中 0.2.0~0.3.9 都是**旧契约**的包
#     （0.3.1 之前连的是 IP 直连入口）。
#   · 旧链接对外公开可达 ⇒ 内测用户可能拿到一个「连不上 / 契约不对」的旧包，
#     表现是「装得上、登录就炸」，比拿不到包更难排查。
#   · 磁盘本身不紧张（19%），所以这不是空间问题，是**分发面收敛**问题。
#
# 保留策略：留最近 N 个版本化文件（当前 + 上一个），
# 因为「上一个版本」是**回滚保险**（发新版本翻车时要能立刻退回去），
# 再往前的版本既不能回滚（不会退两步）也没有留存价值。
#
# ⚠️ 绝不能删 latest.apk 本身（它是软链，删了营销页的下载按钮直接 404）。
set -uo pipefail

REMOTE_DIR="${REMOTE_DIR:-/var/www/stashbox-downloads}"
KEEP="${KEEP:-2}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/stashbox_deploy}"
HOST="${HOST:-root@8.148.9.151}"

echo "→ 保留最近 ${KEEP} 个版本化 APK（目录：${REMOTE_DIR}）"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$HOST" "
set -u
cd '$REMOTE_DIR' || exit 1
before=\$(ls -1 tingxia-*.apk 2>/dev/null | wc -l)
# 按 mtime 从新到旧排，保留前 KEEP 个，其余删除
ls -1t tingxia-*.apk 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r f; do
  rm -f -- \"\$f\" && echo \"  删：\$f\"
done
after=\$(ls -1 tingxia-*.apk 2>/dev/null | wc -l)
echo \"  版本化 APK：\${before} → \${after} 份\"
# 自检：latest.apk 必须还在且仍能解析
if [ -L latest.apk ]; then
  echo \"  latest.apk → \$(readlink -f latest.apk | xargs basename)\"
else
  echo \"  ⚠️ latest.apk 不是软链了！\" >&2
  exit 1
fi
"

echo "→ 公网复核"
HTTP=$(curl -s -o /dev/null -w '%{http_code}' -L https://stashbox.cn/downloads/latest.apk --max-time 30)
SIZE=$(curl -s -o /dev/null -w '%{size_download}' -L https://stashbox.cn/downloads/latest.apk --max-time 30)
echo "  latest.apk → HTTP ${HTTP} / ${SIZE} 字节"
[ "$HTTP" = "200" ] || { echo "✗ 下载不通" >&2; exit 1; }
