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
#   switch-domain.sh ip       → 切回 https://8.148.9.151
#   switch-domain.sh status   → 只看当前状态
set -uo pipefail

ENV_FILE=/etc/stashbox/stashbox.env
GW_DOMAIN=https://stashbox.cn
GW_IP=https://8.148.9.151
STORE_DOMAIN=https://stashbox.cn/audio
STORE_IP=https://8.148.9.151/audio

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
  domain|ip) ;;
  *) echo "用法: $0 domain|ip|status"; exit 2 ;;
esac

if [ "$1" = "domain" ]; then
  GW="$GW_DOMAIN"; STORE="$STORE_DOMAIN"
else
  GW="$GW_IP"; STORE="$STORE_IP"
fi

echo "=== 切到 $GW ==="
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"

sed -i "s#^PUBLIC_GATEWAY_URL=.*#PUBLIC_GATEWAY_URL=$GW#" "$ENV_FILE"
sed -i "s#^LOCAL_STORAGE_PUBLIC_URL=.*#LOCAL_STORAGE_PUBLIC_URL=$STORE#" "$ENV_FILE"
echo "  ✓ env 已改"

# 存量音频：只改 host，路径（含内容 hash）不动
sudo -u stashbox psql stashbox -t -A -c \
  "update articles set audio_url = replace(audio_url, '^https?://[^/]+', '$GW') where audio_url is not null and audio_url !~ '^$GW'" \
  2>/dev/null | sed 's/^/  articles 更新 /'
sudo -u stashbox psql stashbox -t -A -c \
  "update distilled_articles set audio_url = replace(audio_url, '^https?://[^/]+', '$GW') where audio_url is not null and audio_url !~ '^$GW'" \
  2>/dev/null | sed 's/^/  distilled_articles 更新 /'

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
