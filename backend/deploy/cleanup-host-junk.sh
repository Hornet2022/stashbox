#!/usr/bin/env bash
# 听匣生产服务器：主机垃圾清理（磁盘 / 内存相关）
#
# ══════════════════════════════════════════════════════════════════════
# 为什么要有这个脚本
#
# 这台机器是 **2 GiB 小机器**（物理实测 1613MB，40G 磁盘），
# 而「垃圾」会长得比想象中快：每次探针/调试都会在 /tmp 留下脚本、
# 日志、测试音频、APK……实测一次就攒了 **402 个条目 / 152MB**。
#
# 磁盘看起来还宽裕（19%），但**内存是真紧**（可用只有 ~670MB），
# 而「让垃圾不再长回来」比「清一次」重要。
#
# ══════════════════════════════════════════════════════════════════════
# 踩过的坑：**别用 systemd-tmpfiles 来清 /tmp**
#
# 2026-10-25 实测：Ubuntu 的 /usr/lib/tmpfiles.d/tmp.conf 写的是
#   `D /tmp 1777 root root 30d`
# 而 tmpfiles.d(5) 里 `D` 的定义是「**当 --remove 被使用时**才清目录内容」——
# 而 systemd-tmpfiles-clean.timer 跑的是 **--clean** ⇒
# **那条 30d 规则对定时清理根本不生效**。
# 实测：/tmp 攒了 402 个条目跨 4 天没被清过；放一个 30 天前的文件进去
# 跑 `systemd-tmpfiles --clean` 也**没被删**。
#
# 换 `e` 类型（man 页说「按 age 清内容、--clean 时生效」）——
# **实测仍然不删**（systemd 255）：30 天前的文件照样留着，
# 而把 /tmp 自身 mtime 设旧、文件保持新鲜时文件又留着
# ⇒ 说明 age 是按条目判的，但就是没删（与 x 排除规则无关，已对照实验排除）。
#
# ⇒ 结论：/tmp 的清理**自己做**，别指望 tmpfiles。下面的 find 就是为此写的。
#
# ══════════════════════════════════════════════════════════════════════
# 绝对不碰的东西（每一条都有理由）
#
#   · /tmp/systemd-private-*  ← systemd 给 PrivateTmp=true 的单元建的私有 /tmp。
#     五个服务单元全是 PrivateTmp=true ⇒ 它们用的是这一套 ⇒ 删了服务当场挂。
#   · /tmp/aliyun_assist_service.sock ← 阿里云监控 agent 的活动套接字。
#   · /var/lib/stashbox/data   ← **生产音频存储**（STORAGE_PROVIDER=local），
#     文章的 audio_url 指的就是这里的文件 ⇒ 删了老文章没声音。
#   · /root/*backup*           ← 历次维护的回滚保险（删了不可逆）。
#   · /var/log/journal         ← 只按容量压到上限，**不删**（journald 自带
#     SystemMaxUse=200M 策略，超过它自己会回收）。
set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

#: /tmp 里超过多少天的东西算垃圾。
#:
#: 取 7 天而不是更短：探针残留的典型寿命是「一次会话内」，
#: 而 7 天足够覆盖「人还没回来处理」的窗口。
TMP_AGE_DAYS="${TMP_AGE_DAYS:-7}"

#: journal 压到多大（journald 自己的上限是 200M，这里留一半余量）。
JOURNAL_MAX="${JOURNAL_MAX:-100M}"

#: 部署目录（用来找 __pycache__）。
DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/stashbox/stashbox}"

LOG=/var/log/stashbox/host-cleanup.log
# 日志目录可能没权限（本地开发机 / 非 root）⇒ 建不出来也不能让脚本挂掉
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

say() {
  local line
  line="$(date '+%F %T') $*"
  printf '%s\n' "$line"
  [ -d "$(dirname "$LOG")" ] && printf '%s\n' "$line" >> "$LOG" 2>/dev/null || true
}

say "════ 主机清理开始（dry-run=${DRY_RUN}）════"

# ── 1. /tmp：超过 $TMP_AGE_DAYS 天的条目 ────────────────────────────────
#
# 用 find 而不是 shell 通配：`/tmp` 里可能有上千个条目，
# 而 `-mtime +N` 判的是**条目自身**的 mtime（不是 /tmp 的 mtime）。
# 排除项用 `! -name` 写在同一条 find 里 ⇒ 排除与筛选在同一层判定，
# 不存在「先列出再过滤」那种漏网。
#
# ⚠️ **计数要单独数一遍**：`-exec rm -rf {} + | wc -l` 数不到东西
# （rm 不往 stdout 写任何东西 ⇒ 永远数出 0，实测踩过：明明删了 2 项却报 0）。
# ⇒ 先用 `-print` 数，再删。
tmp_find() {
  find /tmp -xdev -mindepth 1 -maxdepth 1 -mtime "+$TMP_AGE_DAYS" \
    ! -name 'systemd-private-*' \
    ! -name 'aliyun_assist_service.sock' \
    "$@" 2>/dev/null
}
tmp_removed=$(tmp_find -print | wc -l | tr -d ' ')
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] /tmp 里超过 ${TMP_AGE_DAYS} 天的条目（排除 systemd-private-* 与阿里云套接字）："
  tmp_find -print | head -20 | sed 's/^/    /'
else
  tmp_find -exec rm -rf -- {} +
fi
say "  /tmp：清掉 ${tmp_removed} 项（> ${TMP_AGE_DAYS} 天）"

# ── 2. apt 缓存 ─────────────────────────────────────────────────────────
# 装完的 .deb 包留着没用；实测能占 291MB。
# 另：/etc/apt/apt.conf.d/10periodic 的 APT::Periodic::AutocleanInterval
# 默认是 "0"（关）⇒ 本机 2026-10-25 已改成 "7"（每周），apt-daily.timer 会跑。
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] apt-get clean（当前缓存 $(du -sm /var/cache/apt 2>/dev/null | cut -f1)MB）"
else
  apt-get clean >/dev/null 2>&1
  say "  apt 缓存：清到 $(du -sm /var/cache/apt 2>/dev/null | cut -f1)MB"
fi

# ── 3. journal 压容量（不删内容语义，只是回收归档）──────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] journalctl --vacuum-size=${JOURNAL_MAX}（当前 $(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | head -1)）"
else
  journalctl --vacuum-size="$JOURNAL_MAX" >/dev/null 2>&1
  say "  journal：压到 $(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | head -1)（上限 ${JOURNAL_MAX}）"
fi

# ── 4. 部署目录里的陈旧 __pycache__ ────────────────────────────────────
# 生产跑 PYTHONDONTWRITEBYTECODE=1 ⇒ 不会再生长；
# 现有的这些是从开发机整树复制过来的陈旧字节码。
pycache_n=$(find "$DEPLOY_ROOT" -name '__pycache__' -type d 2>/dev/null | wc -l)
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] $DEPLOY_ROOT 下的 __pycache__：${pycache_n} 个"
else
  if [ "$pycache_n" -gt 0 ]; then
    find "$DEPLOY_ROOT" -name '__pycache__' -type d -prune -exec rm -rf -- {} + 2>/dev/null
  fi
  say "  __pycache__：清掉 ${pycache_n} 个"
fi

# ── 5. 凭据备份文件：只留最新一份 ─────────────────────────────────────────
#
# 2026-10-25 实测：`/etc/stashbox/` 下堆了 **16 份** `stashbox.env.bak*`，
# 每一份都是 env 的完整副本。逐份比对密钥哈希后确认：**16/16 份都含
# 当前仍然有效的** JWT_SECRET / POSTGRES_PASSWORD / REDIS_PASSWORD /
# CALLBACK_SHARED_SECRET / ADMIN_BOOTSTRAP_PASSWORD
# ⇒ 不是「旧密钥已轮换、留着无害」，而是 **16 份活凭据的副本**。
#
# 为什么危险：这些文件是 root:stashbox 0640 ⇒ **stashbox 组内任何进程可读**，
# 而 5 个服务单元正是以 stashbox 用户跑的。
# ⇒ 任何一处服务被攻破，攻击者拿到的不是「一个数据库密码」，
#   而是「能签任意账号 JWT 的密钥」+「能直连 PG/Redis 的密码」。
#
# 为什么不全删：env 改坏了要能回滚 ⇒ **保留最新 1 份原样**。
# 为什么不用「备份时把密钥掩码」：那样回滚出来的配置带着占位密钥，
# 看着能回滚、实际起不来 —— 比没有备份更危险。
#
# 为什么不在 cron 里做「更旧就删」：这 16 份是 10/21~10/23 几天内堆出来的
# （部署时手工 cp 留下的），不做规则就会再长回来。
ENV_DIR="${ENV_DIR:-/etc/stashbox}"
ENV_BACKUP_KEEP="${ENV_BACKUP_KEEP:-1}"
#: 生产唯一真正的 env（systemd 的 EnvironmentFile 指的就是它）。
ENV_CURRENT="${ENV_CURRENT:-$ENV_DIR/stashbox.env}"

env_backups=$(ls -1t "$ENV_DIR"/stashbox.env.bak* 2>/dev/null || true)
env_backup_n=$(printf '%s\n' "$env_backups" | grep -c . || true)
if [ "$env_backup_n" -gt 0 ]; then
  # 按 mtime 从新到旧排，第 1 份保留，其余删除。
  env_removed=0
  keep=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    keep=$((keep + 1))
    if [ "$keep" -le "$ENV_BACKUP_KEEP" ]; then
      continue
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "  [dry-run] 删旧 env 备份：$(basename "$f")"
      env_removed=$((env_removed + 1))
    else
      rm -f -- "$f" && env_removed=$((env_removed + 1))
    fi
  done <<EOF
$env_backups
EOF
  say "  env 凭据备份：${env_backup_n} 份 → 留 ${ENV_BACKUP_KEEP} 份，删 ${env_removed} 份"
fi

# ── 6. 调试脚本的固定去处 ───────────────────────────────────────────────
#
# 为什么专门给它一个目录：调试脚本（探针 / 验证 / 临时比对）是**一次性的**，
# 但它们的寿命只有几小时，而 /tmp 的清理规则是「>7 天」⇒
# 调试脚本会赖在 /tmp 里直到下周（实测积了 12 个）。
#
# 用泛化规则去猜「哪个文件像调试脚本」不靠谱（误删正常的临时文件）。
# ⇒ 给它们一个**专名目录**：调试脚本一律写进 ${DEBUG_TMP}，
# 清理规则直接清空这个目录 —— 既不会误伤，又保证「调试完不留痕」。
DEBUG_TMP="${DEBUG_TMP:-/tmp/stashbox-debug}"
if [ -d "$DEBUG_TMP" ]; then
  dbg_n=$(find "$DEBUG_TMP" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] 清空调试目录 ${DEBUG_TMP}（${dbg_n} 项）"
  else
    find "$DEBUG_TMP" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null
  fi
  say "  调试目录：${DEBUG_TMP} 清掉 ${dbg_n} 项"
fi

# ── 7. 开发机整树复制带来的凭据/调试残留 ─────────────────────────────────
#
# 2026-10-25 实测：生产 `/opt/stashbox/stashbox/backend/` 下有
# `.env.bak` / `.env.bak2`（各含一整套 OSS/ARK/ANTHROPIC 密钥）
# 和 `_diag_repro.py`（一个**能读 .env 的 JWT_SECRET 签任意账号 token** 的调试脚本）。
#
# 为什么生产不需要它们：生产的真 env 是 `/etc/stashbox/stashbox.env`
# （systemd EnvironmentFile 指定的），`backend/` 下的 `.env*` **根本不会被加载**
# ⇒ 纯冗余，且冗余得很有害（多一份凭据副本）。
# `_diag_repro.py` 则是「签任意账号 JWT」的工具脚本，在生产上没有正当用途。
#
# ⚠️ 只删 `.env.bak*`（备份），**绝不碰 `.env` 本身** ——
# 万一哪天部署方式变了、真的开始读 backend/.env，删了就起不来。
DEPLOY_BACKEND="${DEPLOY_BACKEND:-$DEPLOY_ROOT/backend}"
stale_files=$(find "$DEPLOY_BACKEND" -maxdepth 1 \( -name '.env.bak*' -o -name '_diag_repro.py' \) -type f 2>/dev/null)
stale_n=$(printf '%s\n' "$stale_files" | grep -c . || true)
if [ "$stale_n" -gt 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' "$stale_files" | sed 's/^/  [dry-run] 删：/'
  else
    printf '%s\n' "$stale_files" | grep . | while IFS= read -r f; do rm -f -- "$f"; done
  fi
  say "  开发残留（.env.bak* / _diag_repro.py）：清掉 ${stale_n} 个"
fi

# ── 8. 收尾体检：确认该活着的都还活着 ─────────────────────────────────
if [ "$DRY_RUN" -eq 0 ]; then
  sp=$(find /tmp -maxdepth 1 -name 'systemd-private-*' 2>/dev/null | wc -l)
  say "  自检：systemd-private 目录 ${sp} 个（必须 ≥ 2）"
  [ -S /tmp/aliyun_assist_service.sock ] && say "  自检：阿里云套接字仍在 ✅"
  # 凭据相关的自检：当前 env 必须还在，备份只能剩 ENV_BACKUP_KEEP 份。
  [ -f "$ENV_CURRENT" ] && say "  自检：当前 env 在（$(basename "$ENV_CURRENT")）✅" \
                        || say "  自检：⚠️ 当前 env 不见了（${ENV_CURRENT}）"
  left=$(ls -1 "$ENV_DIR"/stashbox.env.bak* 2>/dev/null | wc -l | tr -d ' ')
  say "  自检：env 备份剩 ${left} 份（应 = ${ENV_BACKUP_KEEP}）"
  say "  结果：/tmp $(du -sm /tmp 2>/dev/null | cut -f1)MB / 磁盘 $(df -h / | awk 'NR==2{print $5}') / 可用内存 $(free -m | awk '/^Mem:/{print $7}')MB"
fi

say "════ 主机清理结束 ════"
