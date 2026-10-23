#!/usr/bin/env bash
# 剪藏抓取的**健康基线**（journald 口径）。
#
# ## 为什么是日志口径而不是 Prometheus
#
# 仓里**已经有** Prometheus 指标（`common/fetch_metrics.py` 的
# `stashbox_fetch_attempts_total` / `stashbox_fetch_escalations_total` /
# `stashbox_fetch_duration_seconds`），采集点也接好了
# （`fetchers/pipeline.py`），生产 `/metrics` 端点也确实暴露它们。
#
# 但两件事让 Prometheus 口径在这台机器上**不可用**：
#
# 1. **没有 scrape 端**：生产没装 Prometheus server（2g 机器刻意不装），
#    指标值只活在进程内存里 —— content-service 一重启就**全部归零**。
# 2. **没人看**：`技术方案_v1.md` 记着 2026-10-03 的决定「先看指标跑一周
#    再定要不要上代理」，配套的告警 `CaptureBotChallengeRatioHigh`
#    （>5% 持续 2h）在当前部署形态下**根本不存在**。
#
# 而抓取链路的**结构化日志**（journald）带齐了归因需要的全部维度：
# 命中哪一节通道（tier）、失败在哪个错误码（code）、升级了几次。
# 而且 journald 是**持久化**的 —— 进程重启不丢。
#
# ⇒ 「跑一周看指标」这件事，**一条 journalctl 聚合就能做**，不必装任何东西。
#
# ## 用法
#
#     bash backend/scripts/fetch_health.sh              # 近 7 天
#     bash backend/scripts/fetch_health.sh --since 2026-10-20
#     bash backend/scripts/fetch_health.sh --host root@8.148.9.151
#
# ## 怎么读这份输出
#
# · **失败率**：`(终态失败) / (成功 + 终态失败)`。
#   其中 `ssrf_blocked` 与 `unsupported` **不算产品失败**
#   （前者是 SSRF 防护正确拦下内网地址，后者是用户剪了不支持的链接）。
# · **parse 是微信这一类的大头**：`wechat article body (#js_content) not found`
#   ⇒ 服务端按 IP 判定返回了空壳页。修复前它占失败的一半；
#   2026-10-23 起用 `?nwr_flag=1` 重试自愈（见 `fetchers/wechat.py`）。
# · **auth 高**：被判定为非官方客户端 → 这类才是「该考虑代理/换出口」的信号。
# · **decision 规则**（`技术方案_v1.md` §7.x）：
#   bot_challenge 占比 > 5% 且持续 2h → 才值得上代理。
#   低于这个量级，代理的钱应该花在别处。

set -uo pipefail

SINCE="7 days ago"
HOST=""
SERVICE="stashbox-content-service"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --service) SERVICE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -n "$HOST" ]]; then
  # 远端执行（默认本机就在生产机上时不需要）
  cmd="journalctl -u '${SERVICE}' --since '${SINCE}' --no-pager -o short-iso"
  if [[ -n "${STASHBOX_SSH_KEY:-}" ]]; then
    ssh -i "$STASHBOX_SSH_KEY" -o StrictHostKeyChecking=no "$HOST" "$cmd" > /tmp/_fetch_health.log 2>/dev/null
  else
    ssh -o StrictHostKeyChecking=no "$HOST" "$cmd" > /tmp/_fetch_health.log 2>/dev/null
  fi
  LOG=/tmp/_fetch_health.log
else
  journalctl -u "${SERVICE}" --since "${SINCE}" --no-pager -o short-iso 2>/dev/null > /tmp/_fetch_health.log
  LOG=/tmp/_fetch_health.log
fi

if [[ ! -s "${LOG}" ]]; then
  echo "没有日志（service=${SERVICE} since=${SINCE}）—— 确认服务名与时间窗" >&2
  exit 1
fi

echo "=== 剪藏抓取健康基线（service=${SERVICE} since=${SINCE}）==="

ok=$(grep -c 'fetch_ok' "${LOG}" || true)
# capture_fetch_fail 是**终态**失败汇总（escalate 之后的最终结论）
fail=$(grep -c 'capture_fetch_fail' "${LOG}" || true)
# 不算产品失败的两类：SSRF 防护正确拦下 / 用户剪了不支持的链接
not_ours=$(grep 'capture_fetch_fail' "${LOG}" | grep -cE 'code=fetcher\.(ssrf_blocked|unsupported)' || true)
esc=$(grep -c 'fetch_escalate\|fetch_parse_escalate' "${LOG}" || true)
wechat_shell=$(grep -c 'js_content' "${LOG}" || true)

total=$((ok + fail))
if [[ "${total}" -gt 0 ]]; then
  pct=$(awk -v a="${fail}" -v b="${total}" 'BEGIN{printf "%.1f", a*100/b}')
  pct_real=$(awk -v a="$((fail - not_ours))" -v b="${total}" 'BEGIN{printf "%.1f", (b>0? a*100/b : 0)}')
else
  pct="n/a"; pct_real="n/a"
fi

printf '  成功            %5d\n' "${ok}"
printf '  终态失败        %5d  (其中 %d 次不算产品失败：ssrf_blocked/unsupported)\n' "${fail}" "${not_ours}"
printf '  降级链升级      %5d\n' "${esc}"
printf '  微信空壳(js_content) %4d\n' "${wechat_shell}"
printf '  ---\n'
printf '  失败率          %5s%%   （扣掉不算产品失败的：%s%%）\n' "${pct}" "${pct_real}"

echo
echo "=== 失败错误码分布 ==="
grep 'capture_fetch_fail' "${LOG}" \
  | grep -o 'code=fetcher\.[a-z_]*' \
  | sort | uniq -c | sort -rn | sed 's/^/  /'
if ! grep -q 'capture_fetch_fail' "${LOG}"; then
  echo "  （无终态失败）"
fi

echo
echo "=== 命中通道分布（fetch_ok）==="
grep 'fetch_ok' "${LOG}" | grep -o 'tier=[a-z]*' | sort | uniq -c | sed 's/^/  /'
if ! grep -q 'fetch_ok' "${LOG}"; then
  echo "  （无成功记录）"
fi

echo
echo "=== 每日分布（成功 / 终态失败）==="
grep -E 'fetch_ok|capture_fetch_fail' "${LOG}" \
  | awk '{d=substr($1,1,10); if (/fetch_ok/) ok[d]++; else fail[d]++}
         END{for (d in ok) seen[d]=1; for (d in fail) seen[d]=1;
              for (d in seen) printf "  %s  ok=%-4d fail=%-4d\n", d, ok[d], fail[d]}' \
  | sort

rm -f "${LOG}"
echo
echo "口径说明：journald（持久化，带 tier + 错误码）—— 见本脚本头部注释。"
