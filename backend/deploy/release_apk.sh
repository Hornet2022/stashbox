#!/usr/bin/env bash
# 发版一个 Android APK 到生产，并把所有「当前版本」的事实同步过去。
#
# ─────────────────────────────────────────────────────────────────
# 为什么要有这个脚本
#
# 发版这件事此前散成三步手工操作（scp APK → sed env → 重启服务），
# 而**营销页完全不在其中** —— 于是页面的下载入口一直停在 v0.3.1，
# 服务上却已经是 0.3.6，中间漏了 5 个版本（2026-10-23 实测）。
#
# 根因不是「忘了改」，是**「当前版本」这件事没有唯一真相源**：
#   · 营销页把 APK 文件名写死在 HTML 里（改一次要动 6 处）
#   · 服务端的版本号在 /etc/stashbox/stashbox.env
#   · 实际文件在 /var/www/stashbox-downloads/
# 三处各说各的，谁也不校验谁。
#
# 现在的分工：
#   · **真相源 = env 里的三个版本变量**（服务重启后生效，版本接口读它）
#   · /downloads/latest.apk 软链 → 指向当前版本的 APK（页面用它下载）
#   · 营销页的版本号文字 → 运行时从 /api/v1/app/version 取，不写死
# 本脚本一次做完：上传 → 软链 → env → 重启 → **校验**。
#
# 校验不是装饰：软链指错、env 写错、上传了半个文件，
# 这三种错在「页面上看起来一切正常」时最难发现，所以脚本最后会实际下载一次比对哈希。
#
# 用法：
#   ./release_apk.sh <apk 路径> <版本名> <versionCode>
# 例：
#   ./release_apk.sh ~/work/stashbox-android/app/build/outputs/apk/release/app-release.apk 0.3.6 9
#
# 幂等：同样的参数重复跑，结果一样。
set -euo pipefail

APK_PATH="${1:?用法: release_apk.sh <apk 路径> <版本名> <versionCode>}"
VERSION_NAME="${2:?用法: release_apk.sh <apk 路径> <版本名> <versionCode>}"
VERSION_CODE="${3:?用法: release_apk.sh <apk 路径> <版本名> <versionCode>}"

HOST="${STASHBOX_HOST:-8.148.9.151}"
SSH_KEY="${STASHBOX_SSH_KEY:-$HOME/.ssh/stashbox_deploy}"
REMOTE_DIR="/var/www/stashbox-downloads"
REMOTE_APK="${REMOTE_DIR}/tingxia-${VERSION_NAME}.apk"
ENV_FILE="/etc/stashbox/stashbox.env"

# 对外的下载域名（2026-10-23 ICP 备案通过后从 IP 切回域名）。
#
# 之前是 https://8.148.9.151/downloads —— 因为未备案时域名的大流量会被
# 云厂商限制（nginx 的 /downloads/ 也是 302 到 IP）。备案通过后：
#   · nginx 的域名入口改成**直出**（patch-nginx-downloads-redirect.py --direct）；
#   · 下发地址也跟着用域名 —— 让用户看到的地址与实际线路一致。
#
# ⚠️ SSH 仍然走 IP（HOST）：那是运维通道，与用户下载无关。
DOWNLOAD_BASE="${STASHBOX_DOWNLOAD_BASE:-https://stashbox.cn/downloads}"

[[ -f "$APK_PATH" ]] || { echo "✗ 找不到 APK：$APK_PATH" >&2; exit 1; }
[[ "$VERSION_CODE" =~ ^[0-9]+$ ]] || { echo "✗ versionCode 必须是整数：$VERSION_CODE" >&2; exit 1; }

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "root@${HOST}")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=no)

echo "→ 上传 APK（${VERSION_NAME} / code ${VERSION_CODE}）"
"${SCP[@]}" "$APK_PATH" "root@${HOST}:${REMOTE_APK}"
"${SSH[@]}" "chown www-data:www-data '${REMOTE_APK}'"

echo "→ 更新 latest.apk 软链（营销页的下载入口指向它）"
# 用相对软链（ln -sfn），别用绝对路径：目录整体搬走时软链不会烂。
"${SSH[@]}" "cd '${REMOTE_DIR}' && ln -sfn 'tingxia-${VERSION_NAME}.apk' latest.apk && chown -h www-data:www-data latest.apk"

echo "→ 更新 env 里的版本变量"
"${SSH[@]}" "sed -i \\
  -e 's|^STASHBOX_APP_DOWNLOAD_URL=.*|STASHBOX_APP_DOWNLOAD_URL=${DOWNLOAD_BASE}/tingxia-${VERSION_NAME}.apk|' \\
  -e 's|^STASHBOX_APP_LATEST_VERSION_CODE=.*|STASHBOX_APP_LATEST_VERSION_CODE=${VERSION_CODE}|' \\
  -e 's|^STASHBOX_APP_LATEST_VERSION_NAME=.*|STASHBOX_APP_LATEST_VERSION_NAME=${VERSION_NAME}|' \\
  '${ENV_FILE}'"

echo "→ 重启 user-service（版本接口由它提供）"
"${SSH[@]}" "systemctl restart stashbox-user-service && sleep 6"

echo "→ 校验"
LOCAL_SHA="$(shasum -a 256 "$APK_PATH" | awk '{print $1}')"
REMOTE_SHA="$("${SSH[@]}" "sha256sum '${REMOTE_APK}' | cut -d' ' -f1" | tr -d '\r')"
[[ "$LOCAL_SHA" == "$REMOTE_SHA" ]] || { echo "✗ 上传的 APK 与本地不一致（${LOCAL_SHA} vs ${REMOTE_SHA}）" >&2; exit 1; }
echo "  ✓ 上传文件哈希一致 ${LOCAL_SHA:0:12}…"

# 软链解析到的必须是当前版本（不是上一版 —— 软链没更新是最容易犯的错）
LINK_TARGET="$("${SSH[@]}" "readlink -f '${REMOTE_DIR}/latest.apk'" | tr -d '\r')"
[[ "$LINK_TARGET" == "$REMOTE_APK" ]] || { echo "✗ latest.apk 指向了 ${LINK_TARGET}，应为 ${REMOTE_APK}" >&2; exit 1; }
echo "  ✓ latest.apk → ${LINK_TARGET##*/}"

# 版本接口必须说出同一个版本
API_JSON="$("${SSH[@]}" "curl -s https://api.stashbox.cn/api/v1/app/version" | tr -d '\r')"
echo "$API_JSON" | grep -q "\"latest_version_name\":\"${VERSION_NAME}\"" \
  || { echo "✗ 版本接口没返回 ${VERSION_NAME}：${API_JSON}" >&2; exit 1; }
echo "$API_JSON" | grep -q "\"latest_version_code\":${VERSION_CODE}" \
  || { echo "✗ 版本接口的 code 不是 ${VERSION_CODE}：${API_JSON}" >&2; exit 1; }
echo "  ✓ 版本接口 = ${VERSION_NAME} / code ${VERSION_CODE}"

# 营销页的下载入口必须真的能下到东西（不是 HTML —— 那个坑踩过两次：
# /downloads/ 路由漏掉时 try_files 会返回 index.html，状态码还是 200）
#
# ⚠️ 用**域名**校验，不要用 IP：域名才是用户实际点的那个入口
# （备案通过前 nginx 会 302 到 IP，用域名测会测到「302 正常」却漏掉
#  真正的问题；现在改成直出，域名测才等于用户看到的路径）。
HTTP_CODE="$("${SSH[@]}" "curl -s -o /dev/null -w '%{http_code}' '${DOWNLOAD_BASE}/latest.apk'" | tr -d '\r')"
[[ "$HTTP_CODE" == "200" ]] || { echo "✗ ${DOWNLOAD_BASE}/latest.apk 返回 ${HTTP_CODE}" >&2; exit 1; }
SIZE="$("${SSH[@]}" "curl -s -o /dev/null -w '%{size_download}' '${DOWNLOAD_BASE}/latest.apk'" | tr -d '\r')"
LOCAL_SIZE="$(wc -c < "$APK_PATH" | tr -d ' ')"
[[ "$SIZE" == "$LOCAL_SIZE" ]] || { echo "✗ 下载大小 ${SIZE} 与本地 ${LOCAL_SIZE} 不符" >&2; exit 1; }
echo "  ✓ ${DOWNLOAD_BASE}/latest.apk 可下载（${SIZE} 字节，域名直出）"

# 域名入口必须**直出**（不 302）。备案通过后如果还是 302，说明 nginx 没切干净 ——
# 那一态是备案未过期用的，用户点的域名会被转到 IP，语义与地址都不一致。
REDIRECTED="$("${SSH[@]}" "curl -s -o /dev/null -w '%{redirect_url}' '${DOWNLOAD_BASE}/latest.apk'" | tr -d '\r')"
[[ -z "$REDIRECTED" ]] || { echo "✗ 域名入口仍在 302 到 ${REDIRECTED}（应直出）" >&2; exit 1; }
echo "  ✓ 域名入口直出（无 302）"

# ── 收尾：清掉过老的版本化 APK ───────────────────────────────────────────
#
# 为什么发版要顺手清：/downloads/ 是 nginx alias 静态直出 ⇒ **目录里有什么就能下载什么**。
# 内测期每发一版就多一个版本化文件，实测已积到 13 个（约 50MB），
# 其中 0.2.0~0.3.9 都是**旧契约**的包（0.3.1 之前连的是 IP 直连入口），
# 而且旧链接对外公开可达 ⇒ 内测用户可能拿到一个「装得上、登录就炸」的旧包。
#
# 保留最近 2 个：当前版本 + 上一版（上一版是**回滚保险** —— 发新版本翻车时要能立刻退回去；
# 再往前的版本既不会退到、也没有留存价值）。
#
# 放在**所有校验之后**做：万一前面任何一步失败就中止了，
# 此时不该顺手删掉可能还要回滚的旧包。
echo "→ 清理过老的版本化 APK（保留最近 ${APK_KEEP:-2} 个）"
if KEEP="${APK_KEEP:-2}" bash "$(dirname "$0")/cleanup-old-apks.sh" 2>&1 | sed 's/^/  /'; then
  :
else
  echo "  ⚠️ 清理旧 APK 失败（不影响本次发版，新包已经上线）" >&2
fi

echo
echo "✅ 发版完成：${VERSION_NAME}（code ${VERSION_CODE}）"
echo "   下载入口：${DOWNLOAD_BASE}/latest.apk"
