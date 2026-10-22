#!/usr/bin/env bash
# 切到域名（或切回 IP）—— 一条命令完成全部改动。
#
# ## 要改的四处（缺一不可）
#   1. env：PUBLIC_GATEWAY_URL / LOCAL_STORAGE_PUBLIC_URL
#      → 决定**新产出**的 audio_url 用什么 host
#   2. DB：articles.audio_url + distilled_articles.audio_url
#      → 存量音频的 URL 在库里，改 env 不会追溯
#   3. APK：base URL 是**编译期常量**（-PapiBaseUrl），必须重打
#      → 这一步不在服务器上做，需要在构建机执行
#   4. nginx 片段：域名块已是域名证书；IP 兜底块保持 IP 证书
#      → 本脚本**不动** nginx（两套证书已经并行可用）
#
# ## 用法
#   switch-domain.sh domain   → 切到 https://stashbox.cn
#   switch-domain.sh api      → 切到 https://api.stashbox.cn（App 用的就是这个）
#   switch-domain.sh ip       → 切回 https://8.148.9.151
#   switch-domain.sh status   → 只看当前状态
set -uo pipefail

ENV_FILE=/etc/stashbox/stashbox.env
GW_DOMAIN=https://stashbox.cn
GW_API=https://api.stashbox.cn
GW_IP=https://8.148.9.151
STORE_DOMAIN=https://stashbox.cn/audio
STORE_API=https://api.stashbox.cn/audio
STORE_IP=https://8.148.9.151/audio

# `api` 模式：API 走域名，**音频留在 IP**。
# 为什么要分开：音频是**几分钟的连续大文件传输**（3MB+），遇到网络
# 抖动的概率远高于 API 的小请求；实测某网络会 RST 掉到域名的连接，
# 于是音频播到一半断掉（position 停在 172s，state=ERROR）。
# 音频 URL 由服务端下发（App 不拼地址）→ 改这里就够，不用重打 APK。
# 需要音频也走域名时用 `api-full`。


show() {
  echo "  当前 env："
  grep -E '^(PUBLIC_GATEWAY_URL|LOCAL_STORAGE_PUBLIC_URL)=' "$ENV_FILE" | sed 's/^/    /'
  echo "  库里的 audio_url（各 host 的条数）："
  sudo -u stashbox psql stashbox -t -A -c \
    "select split_part(audio_url,'://',2) as host, count(*) from articles where audio_url is not null group by 1" \
    2>/dev/null | sed 's/^/    /'
  sudo -u stashbox psql stashbox -t -A -c \
    "select split_part(audio_url,'://',2) as host, count(*) from distilled_articles where audio_url is not null group by 1" \
    2>/dev/null | sed 's/^/    /'
}

case "${1:-status}" in
  status) show; exit 0 ;;
  domain|api|api-full|ip) ;;
  *) echo "用法: $0 domain|api|api-full|ip|status"; exit 2 ;;
esac

case "$1" in
  domain)   GW="$GW_DOMAIN"; STORE="$STORE_DOMAIN" ;;
  api)      GW="$GW_API";    STORE="$STORE_IP" ;;      # 音频留 IP，见上面的理由
  api-full) GW="$GW_API";    STORE="$STORE_API" ;;     # 音频也走域名
  *)        GW="$GW_IP";     STORE="$STORE_IP" ;;
esac

echo "=== 切到 $GW ==="
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"

sed -i "s#^PUBLIC_GATEWAY_URL=.*#PUBLIC_GATEWAY_URL=$GW#" "$ENV_FILE"
sed -i "s#^LOCAL_STORAGE_PUBLIC_URL=.*#LOCAL_STORAGE_PUBLIC_URL=$STORE#" "$ENV_FILE"
echo "  ✓ env 已改"

# ── 存量音频：只改 host，路径（含内容 hash）不动 ──────────────────
#
# ⚠️ 要改的是**音频基址**的 host，不是 API 的 host。
#    audio_url = LOCAL_STORAGE_PUBLIC_URL + 相对路径，而 api 模式下
#    API 走域名、音频走 IP（见上面「api」模式的理由）→ 两者 host 不同。
#    早先用 API 的 host 做条件/替换，在两者同源时看不出问题，
#    分开之后就变成「更新 0 行」——静默失败。
#
# ⚠️ 必须用 **regexp_replace**，不能用 replace()：
#    SQL 的 replace() 是**字面字符串**替换，'^https?://[^/]+' 这种正则
#    在它眼里就是一串普通字符，在 URL 里根本匹配不上 →
#    UPDATE 影响 0 行、**不报错** → 存量 URL 静默地留在旧 host 上。
#
# 顺带把 UPDATE 的结果**验**出来：数一下还有多少行不是目标 host，
# 不为 0 就报错 —— 静默交出一张全旧 host 的表比崩溃危险得多。
host_of() { printf '%s' "$1" | sed -E 's#^https?://([^/]+).*#\1#'; }
STORE_HOST=$(host_of "$STORE")
STORE_PREFIX=$(printf '%s' "$STORE" | sed -E 's#^(https?://[^/]+).*#\1#')
STORE_ESC=$(printf '%s' "$STORE_HOST" | sed 's/\./\\./g')

for t in articles distilled_articles; do
  # 用 CTE 显式数出行数：psql 的 -q 会把 "UPDATE 14" 这种状态行
  # （**连行数一起**）抑制掉，-t -A 对 UPDATE 也不输出行数。
  affected=$(sudo -u stashbox psql stashbox -t -A -c \
    "with u as (update $t set audio_url = regexp_replace(audio_url, '^https?://[^/]+', '$STORE_PREFIX') where audio_url is not null and audio_url !~ '^https?://$STORE_ESC/' returning 1) select count(*) from u" \
    2>&1 | grep -vE "locale|LANGUAGE|LC_|perl" | tr -d ' ')
  if ! printf '%s' "$affected" | grep -Eq '^[0-9]+$'; then
    echo "  ✗ $t 更新失败：$affected" >&2
    exit 1
  fi
  echo "  ✓ $t 更新 $affected 行（音频 host → $STORE_HOST）"
done

# 断言：不该再有指向别的 host 的存量 URL
leftover=$(sudo -u stashbox psql stashbox -t -A -c \
  "select count(*) from articles where audio_url is not null and audio_url !~ '^https?://$STORE_ESC/'" \
  2>/dev/null | grep -vE "locale|LANGUAGE|LC_|perl" | tr -d ' ')
if [ "${leftover:-0}" != "0" ]; then
  echo "  ✗ 断言失败：articles 里还有 $leftover 行的 host 不是 $STORE_HOST" >&2
  exit 1
fi

systemctl restart stashbox-ai-service stashbox-content-service stashbox-api-gateway stashbox-user-service
sleep 4
echo "  ✓ 服务已重启"
show
cat <<'EOF'

  ⚠️ 还差一步（不在服务器上）：APK 的 base URL 是编译期常量，
     要用新的 host 重新构建并分发给测试者：
       -PapiBaseUrl=https://stashbox.cn
     构建机的命令见 backend/deploy/README.md。
     APK 没重打之前，App 仍然连旧地址（旧地址也仍可用，是刻意的回退）。
EOF
