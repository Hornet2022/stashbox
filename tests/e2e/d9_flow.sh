#!/usr/bin/env bash
# D9 端到端集成测（CP1.7.2）
#
# 链路：curl（模拟客户端） → api-gateway:8100 → content-service:8102 → ai-service:8103
#   1. 本地签 JWT（user_id=1）
#   2. POST /api/v1/callback/d9-add-article  建文章 + 触发蒸馏
#   3. GET  /api/v1/articles/{id}/status     轮询到 ready（30s 上限）
#   4. GET  /api/v1/articles/{id}/audio-url  拿 OSS 签名 URL + expires_at
#
# 前置：4 个服务已起（bash backend/run_dev.sh）。不依赖 docker。
# 用法：bash tests/e2e/d9_flow.sh   或   make e2e-d9
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_PARENT="$(dirname "$REPO_ROOT")"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8100}"
CONTENT_URL="${CONTENT_URL:-http://localhost:8102}"
AI_URL="${AI_URL:-http://localhost:8103}"
USER_URL="${USER_URL:-http://localhost:8101}"
E2E_USER_ID="${E2E_USER_ID:-}"  # 留空 = 每次跑新开一个用户（配额独立）
POLL_MAX="${POLL_MAX:-15}"      # 次数
POLL_INTERVAL="${POLL_INTERVAL:-2}"  # 秒 → 默认 30s 上限

PY="$REPO_ROOT/backend/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "⚠️  没找到 backend/.venv/bin/python，退回系统 python3（可能缺依赖）"
  PY="python3"
fi

TMP_RUN="$(mktemp -d)"
trap 'rm -rf "$TMP_RUN"' EXIT

fail() { echo "❌ $*" >&2; exit 1; }
pass() { echo "✅ $*"; }
info() { echo "   $*"; }

# 从 JSON 里取字段（非 JSON / 字段缺失 → 空串，不中断）
json_get() {
  printf '%s' "${2-}" | "$PY" -c '
import json, sys
try:
    obj = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
val = obj.get(sys.argv[1], "") if isinstance(obj, dict) else ""
print("" if val is None else val)
' "$1" 2>/dev/null || echo ""
}

# 服务未起 → 直接报错退出（§4.1 自检要求）
precheck() {
  local name="$1" url="$2"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url/healthz" 2>/dev/null || true)
  if [[ "$code" != "200" ]]; then
    fail "$name 未就绪（$url/healthz → HTTP ${code:-000}）。先起服务：nohup bash backend/run_dev.sh > /tmp/dev.log 2>&1 & disown"
  fi
  info "$name ready ($url)"
}

echo "=== E2E D9 流程开始（CP1.7.2）==="
echo "GATEWAY_URL : $GATEWAY_URL"
echo "CONTENT_URL : $CONTENT_URL"
echo "E2E_USER_ID : $E2E_USER_ID"

echo ""
echo "=== Step 0: 服务健康检查 ==="
precheck "api-gateway" "$GATEWAY_URL"
precheck "content-service" "$CONTENT_URL"
precheck "ai-service" "$AI_URL"

# 1. 取 JWT：
#    - E2E_USER_ID 设了 → 本地直接用 create_access_token 签（调试指定用户）
#    - 留空 → 走 wechat-login 新开一个用户（每次跑配额独立，避免免费额度 5/5 用尽后跑不动）
#      注意：开号这一步直连 user-service —— api-gateway 用 functools.partial 注册的路由
#      在 fastapi 0.141 下会把 JSON body 当 Route 模型解析 → 422（CP1.7.1 既有 bug，见 README）。
#      被测链路（D9 / status / audio-url）仍然全部走 gateway。
echo ""
echo "=== Step 0.5: 取 JWT ==="
if [[ -n "$E2E_USER_ID" ]]; then
  JWT=$(cd "$REPO_ROOT" && PYTHONPATH="$REPO_PARENT:$REPO_ROOT" "$PY" -c "
from stashbox.backend.common.auth import create_access_token
print(create_access_token('$E2E_USER_ID'))
") || fail "JWT 签发失败（venv 缺依赖？）"
  [[ -n "$JWT" ]] || fail "JWT 为空"
  pass "JWT 本地签发成功（user_id=$E2E_USER_ID, len=${#JWT}）"
else
  LOGIN_CODE="e2e_$(date +%s)_$$"
  LOGIN_RESP="$(curl -s --max-time 10 -X POST "$USER_URL/api/v1/auth/wechat-login" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"code": "%s"}' "$LOGIN_CODE")")" || fail "curl wechat-login 失败"
  JWT="$(json_get access_token "$LOGIN_RESP")"
  E2E_USER_ID="$(json_get user_id "$LOGIN_RESP")"
  if [[ -z "$JWT" || -z "$E2E_USER_ID" ]]; then
    fail "wechat-login 没返 token/user_id：$LOGIN_RESP"
  fi
  pass "JWT 获取成功（新开测试用户 user_id=$E2E_USER_ID, len=${#JWT}）"
fi

# 2. D9 callback（走 gateway 8100，带 X-Request-ID 验证链路透传）
echo ""
echo "=== Step 1: D9 callback（api-gateway 8100）==="
RID="e2e_test_$(date +%s)_$$"
TEST_URL="https://mp.weixin.qq.com/s/e2e_test_$(date +%s)_$$"
PAYLOAD=$(printf '{"url": "%s", "source": "wechat"}' "$TEST_URL")

HTTP_CODE=$(curl -s -o "$TMP_RUN/d9.body" -D "$TMP_RUN/d9.head" -w '%{http_code}' \
  --max-time 15 -X POST "$GATEWAY_URL/api/v1/callback/d9-add-article" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $JWT" \
  -H "X-Request-ID: $RID" \
  -d "$PAYLOAD") || fail "curl D9 callback 失败（$GATEWAY_URL 连不上？）"

RESP_BODY="$(cat "$TMP_RUN/d9.body")"
echo "$RESP_BODY"
if [[ "$(json_get code "$RESP_BODY")" == "3001" ]]; then
  fail "配额用尽（user_id=${E2E_USER_ID}）：$RESP_BODY
  重跑：E2E_USER_ID=<有额度的用户> bash tests/e2e/d9_flow.sh（留空则每次新开用户）"
fi
if [[ "$HTTP_CODE" != "200" ]]; then
  fail "D9 callback 返 HTTP ${HTTP_CODE}（预期 200）。body: $RESP_BODY"
fi

ARTICLE_ID="$(json_get article_id "$RESP_BODY")"
TASK_ID="$(json_get task_id "$RESP_BODY")"
if [[ -z "$ARTICLE_ID" ]]; then
  fail "D9 callback 没返 article_id：$RESP_BODY"
fi
pass "文章创建成功 article_id=$ARTICLE_ID task_id=${TASK_ID:-null}"

# X-Request-ID 链路：客户端带的 id 应在响应头原样回来
RESP_RID="$(tr -d '\r' < "$TMP_RUN/d9.head" | awk 'tolower($1) == "x-request-id:" {print $2}' | tail -1)"
if [[ "$RESP_RID" != "$RID" ]]; then
  fail "X-Request-ID 未透传（sent=$RID, got=${RESP_RID:-<none>}）"
fi
pass "X-Request-ID 链路透传（${RESP_RID}）"

# 3. 立即查 status（应 pending / distilling）
echo ""
echo "=== Step 2: status（立即）==="
STATUS="$(curl -s --max-time 10 "$GATEWAY_URL/api/v1/articles/$ARTICLE_ID/status" \
  -H "Authorization: Bearer $JWT")" || fail "curl status 失败"
echo "$STATUS"
CURRENT="$(json_get status "$STATUS")"
[[ -n "$CURRENT" ]] || fail "status 端点没返 status 字段：$STATUS"
info "初始状态: $CURRENT"

# 4. 轮询等 ready（最多 POLL_MAX * POLL_INTERVAL 秒）
echo ""
echo "=== Step 3: 轮询 status 等 ready（上限 $((POLL_MAX * POLL_INTERVAL))s）==="
ELAPSED=0
for i in $(seq 1 "$POLL_MAX"); do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((i * POLL_INTERVAL))
  STATUS="$(curl -s --max-time 10 "$GATEWAY_URL/api/v1/articles/$ARTICLE_ID/status" \
    -H "Authorization: Bearer $JWT")" || fail "curl status 失败"
  CURRENT="$(json_get status "$STATUS")"
  info "[t+${ELAPSED}s] status: ${CURRENT:-<empty>}"
  if [[ "$CURRENT" == "ready" ]]; then
    pass "蒸馏完成（${ELAPSED}s）"
    break
  fi
  if [[ "$CURRENT" == "failed" ]]; then
    fail "蒸馏失败（t+${ELAPSED}s）：$STATUS"
  fi
done

if [[ "$CURRENT" != "ready" ]]; then
  fail "超时：$((POLL_MAX * POLL_INTERVAL))s 内未 ready（最后状态 ${CURRENT}）"
fi

# 5. 拿 audio-url
echo ""
echo "=== Step 4: audio-url ==="
AUDIO="$(curl -s --max-time 10 "$GATEWAY_URL/api/v1/articles/$ARTICLE_ID/audio-url" \
  -H "Authorization: Bearer $JWT")" || fail "curl audio-url 失败"
echo "$AUDIO"

AUDIO_URL="$(json_get audio_url "$AUDIO")"
EXPIRES_AT="$(json_get expires_at "$AUDIO")"
DURATION="$(json_get duration_sec "$AUDIO")"
if [[ -z "$AUDIO_URL" ]]; then
  fail "audio-url 为空：$AUDIO"
fi
info "AUDIO_URL    : $AUDIO_URL"
info "EXPIRES_AT   : $EXPIRES_AT"
info "DURATION_SEC : ${DURATION:-0}"

# 6. 校验 OSS 签名 URL 格式（CP1.7 是 mock 签名）
if [[ ! "$AUDIO_URL" =~ ^https://stashbox-audio\.oss-cn-hangzhou\.aliyuncs\.com/.*\.m4a\?Expires=[0-9]+\&OSSAccessKeyId=mock\&Signature=mock$ ]]; then
  fail "AUDIO_URL 格式错误（不是 OSS 签名 URL）：$AUDIO_URL"
fi
pass "AUDIO_URL 格式正确（OSS 签名 URL）"

# 7. 校验 expires_at 在未来 1 小时内（TTL=3600s，留 100s 余量）
#    expires_at 是带时区的 ISO 8601（...+00:00），交给 python 解析，
#    不用 macOS 的 date -j -f（对时区后缀不兼容）
DELTA="$("$PY" -c '
import datetime, sys
raw = sys.argv[1].replace("Z", "+00:00")
try:
    dt = datetime.datetime.fromisoformat(raw)
except ValueError:
    print("nan"); sys.exit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=datetime.timezone.utc)
print(int((dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
' "$EXPIRES_AT")"

if [[ "$DELTA" == "nan" ]]; then
  fail "expires_at 不是合法 ISO 8601：$EXPIRES_AT"
fi
if (( DELTA < 0 || DELTA > 3700 )); then
  fail "expires_at 不合理：${EXPIRES_AT}（delta=${DELTA}s，期望 0~3700）"
fi
pass "expires_at 合理（+$DELTA 秒）"

echo ""
echo "=== ✅ E2E D9 全流程通过 ==="
echo "ARTICLE_ID : $ARTICLE_ID"
echo "TASK_ID    : ${TASK_ID:-null}"
echo "AUDIO_URL  : $AUDIO_URL"
echo "DURATION   : ${DURATION:-0}s"
echo "耗时        : ${ELAPSED}s（蒸馏）+ 请求开销"
exit 0
