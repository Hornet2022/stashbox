#!/usr/bin/env bash
# 本机开发服务日志的轮转（macOS）
#
# ══════════════════════════════════════════════════════════════════════
# 为什么不用 newsyslog
# ══════════════════════════════════════════════════════════════════════
# newsyslog 轮转是**改名**（file → file.1），而本机这些后端服务是
# **手工起的 uvicorn**（不是 launchd 托管），它们持有日志文件的 fd：
#
#   改名 → 进程继续往**已改名**的文件里写 → 归档文件一直长 → 等于没轮转。
#
# newsyslog 能让进程重开文件，但要走 `signal` + `pid` 字段 ——
# 这些进程每次手工起，pid 都变，没有稳定的 pidfile 可指。
#
# 所以用 copytruncate 的思路：**压缩一份 + 截断原文件**。
# 截断不动 inode，fd 照常有效，服务无感（这就是生产 logrotate 里
# copytruncate 解决的是同一个问题）。
#
# ⚠️ macOS 专用：`stat -f%z`（BSD stat）。Linux 上是 `stat -c%s`。
#    生产服务器走的是另一套（logrotate + journald），见 install-log-policy.sh。
#
# ══════════════════════════════════════════════════════════════════════
# 保留策略
# ══════════════════════════════════════════════════════════════════════
#   · 超过 50MB 才处理（平时不动，避免每天无意义地压一遍）
#   · 归档只留**最近 20MB**（gzip 压完 ~2-3MB）——
#     开发日志的价值在「最近发生了什么」，开头的几百 MB 基本是噪音
#   · 只留 1 份归档（.1.gz），下一轮直接覆盖
set -euo pipefail

MAX_BYTES=52428800       # 50MB
KEEP_TAIL_BYTES=20971520 # 归档保留最近 20MB
LOG_GLOB="${1:-/private/tmp/stashbox-*.out}"

rotated=0
for f in $LOG_GLOB; do
  [ -f "$f" ] || continue
  size=$(stat -f%z "$f")
  if [ "$size" -le "$MAX_BYTES" ]; then
    continue
  fi
  # 先取尾部再截断：顺序反了的话，截断后 tail 就没东西可取了
  tail -c "$KEEP_TAIL_BYTES" "$f" | gzip -c > "${f}.1.gz"
  : > "$f"
  gz_size=$(stat -f%z "${f}.1.gz")
  printf '已轮转 %s：%s → 归档 %s（保留最近 %s 字节）\n' \
    "$f" "$size" "$gz_size" "$KEEP_TAIL_BYTES"
  rotated=$((rotated + 1))
done

if [ "$rotated" -eq 0 ]; then
  echo "无需轮转（阈值 ${MAX_BYTES} 字节）"
fi
