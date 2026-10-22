#!/usr/bin/env python3
"""verify_audio_consistency.py — 核对「DB 引用的音频」与「磁盘上的音频文件」是否一致。

## 为什么要有这个脚本（2026-10-22 生产事故）

生产上做过一次「清理孤儿音频」：扫目录 → 列出 DB 没引用的文件 → 移走。
结果**误删了 6 篇仍然在用的文章音频**（用户点播放就是 404）。

根因不是手滑，是**判据本身不完整**：
  - 参考集是手写死的几张表 + 手写死的列，漏了 `article_audio_variants`；
  - 取文件名的方式各家不同（URL 取最后一段、oss_key 去掉 `audio/` 前缀）；
  - **最要命的一条**：脚本在「已经发现有引用缺失」的情况下，仍然按「无主文件」
    清单继续删除 —— 参考集不可信时，「没被引用」这个结论同样不可信。

所以判据必须缝成一份**可审查、可复用、带联锁**的契约：

  1. **参考集自动发现**：查 information_schema，把所有 public 表里叫
     audio_url / oss_key / audio_path 的列全部纳入。手写表名会漂移
     —— 新加一张存音频的表，漏了它就等于「删掉正在用的文件」。
  2. **键归一化唯一**：URL（含 query）→ basename；oss_key（`audio/xxx`）→ basename。
  3. **双向报告**：引用缺失（用户播放 404）与无主文件（占磁盘）分开报。
  4. **联锁**：只要存在引用缺失，就**禁止**任何删除动作 —— 此时
     「无主文件」清单同样不可信，先查清引用缺失再说。
  5. **默认只读**：不改任何东西。`--delete-orphans` 也只把文件**移到隔离目录**
     （不是 rm），且要 `--yes` 才真执行。

## 用法

    # 只读核对（生产巡检用；有引用缺失时退出码非 0）
    python scripts/verify_audio_consistency.py

    # 列出「会被移走」的文件，但不真动
    python scripts/verify_audio_consistency.py --delete-orphans

    # 真移走（先看清单，再加 --yes）
    python scripts/verify_audio_consistency.py --delete-orphans --yes

    # 换目录 / 换隔离区
    python scripts/verify_audio_consistency.py --audio-dir /var/lib/stashbox/data/audio \\
        --quarantine-dir /var/lib/stashbox/data/.quarantine-audio
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── 包路径自举 ─────────────────────────────────────────────────────────
# 代码里全是 `stashbox.backend.*` 这种绝对导入，而 `stashbox` 是**仓库目录名**
# （不是顶层包）—— 所以 sys.path 里要放的是**仓库根的父目录**。
# 直接按 __file__ 推导，换机器/换部署路径都不用改脚本。
#
# ⚠️ 别指望运行期改 PYTHONPATH 生效：sys.path 在解释器启动时就定好了，
#    运行期 os.environ["PYTHONPATH"]=... 对 import 没有任何影响（踩过）。
_REPO_ROOT = Path(__file__).resolve().parents[2]  # .../stashbox
_PKG_PARENT = _REPO_ROOT.parent  # .../<parent>  ← stashbox 包在这里
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))


def _load_env_file() -> None:
    """加载生产 env 文件（systemd 的 EnvironmentFile 不会作用于手动跑的脚本）。

    缺了它 → common.config 在 import 期就抛 ValidationError：
    「jwt_secret 仍为默认值」—— 因为 JWT_SECRET 是靠 env 注入的。
    """
    env_file = os.getenv("STASHBOX_ENV_FILE", "/etc/stashbox/stashbox.env")
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()

# ── 契约常量（改这里，别改调用处）────────────────────────────────────────
# 视为「音频引用」的列名。匹配 information_schema.columns.column_name。
AUDIO_REF_COLUMNS = ("audio_url", "oss_key", "audio_path")
# 音频目录相对于 LOCAL_AUDIO_DIR 的子目录（storage key 形如 "audio/xxx"）。
AUDIO_SUBDIR = "audio"
# 隔离目录默认名（在音频目录下，删除时由脚本创建）。
DEFAULT_QUARANTINE_NAME = ".quarantine-audio"


# ── 纯逻辑（可单测，不碰 IO）────────────────────────────────────────────
def normalize_key(value: str | None) -> str:
    """把各种形态的音频引用归一成「文件名」。

    - `https://api.stashbox.cn/audio/audio/art_x.abc12345.mp3` → `art_x.abc12345.mp3`
    - `https://api.stashbox.cn/audio/audio/art_x.mp3?sig=...`     → `art_x.mp3`
    - `audio/art_x.mp3`（oss_key 形态）                            → `art_x.mp3`
    - `art_x.mp3`（已经是文件名）                                  → `art_x.mp3`
    - 空 / None                                                    → `""`
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    s = s.split("?", 1)[0].split("#", 1)[0]  # 去 query / fragment
    s = s.rstrip("/")
    return s.rsplit("/", 1)[-1]


@dataclass
class Plan:
    """核对结果。"""

    referenced: set[str] = field(default_factory=set)
    on_disk: set[str] = field(default_factory=set)
    missing_refs: set[str] = field(default_factory=set)  # DB 有、磁盘无
    orphan_files: set[str] = field(default_factory=set)  # 磁盘有、DB 无
    ref_sources: dict[str, str] = field(default_factory=dict)  # key → 来源说明

    @property
    def can_delete(self) -> bool:
        """允许删无主文件吗？

        **只要有引用缺失就不允许** —— 参考集不可信时，「这个文件没人引用」
        同样不可信（2026-10-22 生产事故就是这么发生的）。
        """
        return not self.missing_refs

    def block_reason(self) -> str:
        if self.missing_refs:
            return (
                f"存在 {len(self.missing_refs)} 个「DB 引用但磁盘没有」的音频 → "
                "参考集或磁盘状态不可信，禁止删除无主文件（先查清引用缺失）"
            )
        return ""


def build_plan(referenced: dict[str, str], on_disk: set[str]) -> Plan:
    """referenced: {文件名: 来源说明}；on_disk: {磁盘上的文件名}。"""
    plan = Plan(referenced=set(referenced), on_disk=set(on_disk), ref_sources=dict(referenced))
    plan.missing_refs = set(referenced) - set(on_disk)
    plan.orphan_files = set(on_disk) - set(referenced)
    return plan


# ── IO（DB + 磁盘）─────────────────────────────────────────────────────
async def collect_referenced() -> dict[str, str]:
    """自动发现所有音频引用列，取出全部非空引用 → {文件名: 来源说明}。

    自动发现的意义：**手写表名会漂移**。新加一张存音频的表而忘了加进
    硬编码列表，那个表的文件就会被判成「无主文件」而被删掉。
    """
    from sqlalchemy import text

    from stashbox.backend.common.database import AsyncSessionLocal

    refs: dict[str, str] = {}
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "select table_name, column_name from information_schema.columns "
                    "where table_schema = 'public' and column_name = any(:cols) "
                    "order by table_name, column_name"
                ),
                {"cols": list(AUDIO_REF_COLUMNS)},
            )
        ).all()
        for table_name, column_name in rows:
            count = (
                await session.execute(
                    text(f'select count(*) from "{table_name}" where "{column_name}" is not null')
                )
            ).scalar() or 0
            if not count:
                continue
            values = (
                (
                    await session.execute(
                        text(
                            f'select "{column_name}" from "{table_name}" where "{column_name}" is not null'
                        )
                    )
                )
                .scalars()
                .all()
            )
            for v in values:
                key = normalize_key(v)
                if key:
                    refs.setdefault(key, f"{table_name}.{column_name}")
    return refs


def collect_on_disk(audio_dir: Path) -> set[str]:
    if not audio_dir.exists():
        return set()
    return {str(p.relative_to(audio_dir)) for p in audio_dir.rglob("*") if p.is_file()}


def default_audio_dir() -> Path:
    base = Path(os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio"))
    return base / AUDIO_SUBDIR


# ── CLI ────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对 DB 音频引用与磁盘文件的一致性")
    ap.add_argument(
        "--audio-dir", type=Path, default=None, help="音频目录（默认 $LOCAL_AUDIO_DIR/audio）"
    )
    ap.add_argument(
        "--quarantine-dir", type=Path, default=None, help="隔离目录（无主文件移到这里，不删）"
    )
    ap.add_argument(
        "--delete-orphans", action="store_true", help="把无主文件移进隔离目录（默认只 dry-run）"
    )
    ap.add_argument("--yes", action="store_true", help="真执行（不给就是 dry-run）")
    args = ap.parse_args(argv)

    audio_dir = args.audio_dir or default_audio_dir()
    quarantine = args.quarantine_dir or (audio_dir / DEFAULT_QUARANTINE_NAME)

    referenced = asyncio.run(collect_referenced())
    on_disk = collect_on_disk(audio_dir)
    plan = build_plan(referenced, on_disk)

    print(f"音频目录 : {audio_dir}")
    print(f"引用条目 : {len(referenced)}   磁盘文件 : {len(on_disk)}")
    print()
    print(f"[引用缺失] DB 指向但磁盘没有：{len(plan.missing_refs)}")
    for k in sorted(plan.missing_refs):
        print(f"  MISS   {k}   ← {plan.ref_sources.get(k, '?')}")
    print(f"\n[无主文件] 磁盘上没有 DB 引用：{len(plan.orphan_files)}")
    for k in sorted(plan.orphan_files):
        print(f"  ORPHAN {k}")

    if not args.delete_orphans:
        print("\n（只读核对，未改动任何文件）")
        return 1 if plan.missing_refs else 0

    if not plan.can_delete:
        print(f"\n❌ {plan.block_reason()}")
        return 2

    if not plan.orphan_files:
        print("\n没有无主文件，无需处理")
        return 0

    quarantine.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_dir = quarantine / stamp
    if not args.yes:
        print(f"\n（dry-run：真执行会把 {len(plan.orphan_files)} 个文件移到 {target_dir}）")
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for rel in sorted(plan.orphan_files):
        src = audio_dir / rel
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved += 1
        print(f"  MOVED  {rel} → {dst}")
    print(f"\n已移走 {moved} 个无主文件到 {target_dir}（可回滚：移回去即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
