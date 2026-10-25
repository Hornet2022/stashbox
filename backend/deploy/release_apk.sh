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
#   · **真相源 = env 里的五个版本变量**（服务重启后生效，版本接口读它）
#   · /downloads/latest.apk 软链 → 指向当前版本的 APK（页面用它下载）
#   · 营销页的版本号文字 → 运行时从 /api/v1/app/version 取，不写死
# 本脚本一次做完：上传 → 软链 → env → 重启 → **校验**。
#
# 校验不是装饰：软链指错、env 写错、上传了半个文件，
# 这三种错在「页面上看起来一切正常」时最难发现，所以脚本最后会实际下载一次比对哈希。
#
# ─────────────────────────────────────────────────────────────────
# 为什么「更新说明」是**必填**参数（2026-10-25 实测后改的）
#
# 原来这个脚本用 sed 改 3 个变量，漏了 STASHBOX_APP_RELEASE_NOTES（更新说明）
# 和 STASHBOX_APP_PUBLISHED_AT（发布时间）。
# 后果不是"报错"：0.4.2 发出去后，**更新弹窗里写着 0.4.1 的功能清单** ——
# 安卓端 AppUpdateDialog.kt 会渲染这个字段，内测用户更新时读到的就是它。
# 错的说明不会自己暴露，只会让用户读到别人的版本说明。
#
# ⇒ 现在更新说明是第 4 个必填参数：写不出来就别发版（脚本直接报错退出）。
#   5 个变量的写入与回读校验交给 deploy/set_app_version_env.py
#   （它还会拒绝会写坏 systemd EnvironmentFile 的值，比如说明里带换行）。
#
# 用法：
#   ./release_apk.sh <apk 路径> <版本名> <versionCode> <更新说明>
# 例：
#   ./release_apk.sh ~/work/stashbox-android/app/build/outputs/apk/release/app-release.apk 0.4.3 16 "本版功能没有变化：加了两个性能数据采集"
#
# 幂等：同样的参数重复跑，结果一样。
set -euo pipefail

USAGE="用法: release_apk.sh <apk 路径> <版本名> <versionCode> <更新说明>"
APK_PATH="${1:?${USAGE}}"
VERSION_NAME="${2:?${USAGE}}"
VERSION_CODE="${3:?${USAGE}}"
RELEASE_NOTES="${4:?${USAGE}}"

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
# 更新说明不能是空的：更新弹窗会把空说明显示成「服务端未提供更新说明。」
[[ -n "${RELEASE_NOTES//[[:space:]]/}" ]] || { echo "✗ 更新说明不能为空（用户会在更新弹窗里看到它）" >&2; exit 1; }

DOWNLOAD_URL="${DOWNLOAD_BASE}/tingxia-${VERSION_NAME}.apk"

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "root@${HOST}")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=no)

echo "→ 上传 APK（${VERSION_NAME} / code ${VERSION_CODE}）"
"${SCP[@]}" "$APK_PATH" "root@${HOST}:${REMOTE_APK}"
"${SSH[@]}" "chown www-data:www-data '${REMOTE_APK}'"

echo "→ 更新 latest.apk 软链（营销页的下载入口指向它）"
# 用相对软链（ln -sfn），别用绝对路径：目录整体搬走时软链不会烂。
"${SSH[@]}" "cd '${REMOTE_DIR}' && ln -sfn 'tingxia-${VERSION_NAME}.apk' latest.apk && chown -h www-data:www-data latest.apk"

echo "→ 更新 env 里的版本变量（5 个，含用户看得见的更新说明与发布时间）"
# 为什么不直接 sed 改 5 行：
#   · 更新说明是中文用户文案，里面可能有 & | ' 等字符 ——
#     sed 的替换串里 & 代表「整个匹配」、| 是分隔符、' 会闭合引号 ⇒ 静默写坏；
#   · 少写的那两个键正是这次踩的坑（更新说明 / 发布时间）。
# ⇒ 更新说明走 base64，脚本本体喂给远端 python3 的 stdin，
#   真正的写入、备份、去重、原子替换、回读校验都在 set_app_version_env.py 里。
NOTES_B64="$(printf '%s' "$RELEASE_NOTES" | base64 | tr -d '\n')"
if ! ENV_OUT="$("${SSH[@]}" "NOTES_B64='${NOTES_B64}' python3 - --env-file '${ENV_FILE}' --version-name '${VERSION_NAME}' --version-code '${VERSION_CODE}' --download-url '${DOWNLOAD_URL}' --apk-path '${REMOTE_APK}'" \
  < "$(dirname "$0")/set_app_version_env.py")"; then
  echo "✗ 写 env 失败（上面是远端报错），env 未被改动" >&2
  exit 1
fi
printf '%s\n' "$ENV_OUT" | sed 's/^/  /'

# 从远端的回执里取出「这次写进去的发布时刻」与「说明的哈希」，
# 后面拿它们跟版本接口对拍 —— 这样"接口说的"和"我们写的"必须是同一份。
PUBLISHED_AT="$(printf '%s\n' "$ENV_OUT" | sed -n 's/^OK published_at=//p')"
NOTES_SHA="$(printf '%s\n' "$ENV_OUT" | sed -n 's/^OK release_notes_sha256=//p')"
[[ -n "$PUBLISHED_AT" && -n "$NOTES_SHA" ]] || { echo "✗ 远端回执里没有发布时刻/说明哈希：${ENV_OUT}" >&2; exit 1; }

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

# 版本接口必须说出同一个版本 —— 而且 5 个字段都要对上。
#
# 为什么改成用 python 解析 JSON 而不是 grep：
#   · JSON 里的中文是 \uXXXX 转义，grep 比的是"出现过"，不是"就是它"；
#   · 更新说明里可能有引号/反斜杠，grep 的模式会被它们带偏。
# 判据是**逐字段相等**：接口说的必须就是我们刚写进 env 的那份。
API_JSON="$("${SSH[@]}" "curl -s https://api.stashbox.cn/api/v1/app/version" | tr -d '\r')"
if ! printf '%s' "$API_JSON" | \
  EXPECTED_NAME="$VERSION_NAME" \
  EXPECTED_CODE="$VERSION_CODE" \
  EXPECTED_URL="$DOWNLOAD_URL" \
  EXPECTED_PUBLISHED_AT="$PUBLISHED_AT" \
  EXPECTED_NOTES_SHA="$NOTES_SHA" \
  python3 -c '
import hashlib, json, os, sys

data = json.load(sys.stdin)
bad = []


def check(key, want):
    got = data.get(key)
    if got != want:
        bad.append("%s: 接口返回 %r，应为 %r" % (key, got, want))


check("latest_version_name", os.environ["EXPECTED_NAME"])
check("latest_version_code", int(os.environ["EXPECTED_CODE"]))
check("download_url", os.environ["EXPECTED_URL"])
check("published_at", os.environ["EXPECTED_PUBLISHED_AT"])

# 更新说明按哈希比：逐字节一致，不受 JSON 转义/换行影响
notes = data.get("release_notes") or ""
sha = hashlib.sha256(notes.encode("utf-8")).hexdigest()
if sha != os.environ["EXPECTED_NOTES_SHA"]:
    bad.append(
        "release_notes: 接口返回的说明哈希 %s… 与写入的 %s… 不符"
        % (sha[:12], os.environ["EXPECTED_NOTES_SHA"][:12])
    )

if bad:
    sys.stderr.write("\n".join(bad) + "\n")
    sys.exit(1)
'; then
  echo "✗ 版本接口的 5 个字段与本次发版对不上（用户会读到错的说明）：${API_JSON}" >&2
  exit 1
fi
echo "  ✓ 版本接口 5 个字段与本次发版一致（${VERSION_NAME} / code ${VERSION_CODE}，说明与发布时间已核对）"

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
