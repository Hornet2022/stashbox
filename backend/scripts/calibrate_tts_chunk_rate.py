#!/usr/bin/env python3
"""calibrate_tts_chunk_rate.py — 标定「分块语速」的噪声带（判据阈值的依据）。

## 这个脚本存在的理由

`app/services/tts/openai.py` 里有一条判据阈值：

    _TRUNCATION_CHARS_PER_SEC = 11.0   # 所有块长统一用这一档

⚠️ **2026-10-25 变更**：原来有两条（6.8 / 8.0，按长度分档），
   现在合并成一条 —— 因为实测 **cps 与块长无关**（只与文本内容有关），
   分档是在给不存在的结构编理由。

这个数字**不是拍出来的**，是量出来的 —— 而量它们的工具原先是一次性脚本
（/tmp 里的探针），量完就没了。阈值于是变成「注释里的一个数字」：
下一个想改它的人没法复现依据，只能选择信或不信。

本脚本把量法固化成可复现的工具：**换个人、换个时间、换个 provider，
都能重跑一遍拿到自己的带宽数据**，再决定阈值。

⚠️ **换模型 / 换上游后必须重跑**：2026-10-24 的阈值 6.8 是给
   **Qwen3-TTS-12Hz-1.7B-Base** 标的（正常 4.80~6.45 字/秒）；
   生产换成 **CustomVoice** 后正常上界涨到 **9.65** ⇒ 6.8 落进正常区间内部
   ⇒ 24h 内误判 107 次 ⇒ 凭空多出上百个音色接缝。

## 方法

1. 从生产库里取**真实稿件**（已经过 LLM 改写的稿子 —— 改写会改变句读结构，
   直接拿原文切出来的块不具代表性）；
2. 用**生产同一条**切块逻辑（`split_into_chunks(text, chunk_chars)`）；
3. 按字数区间（`--min-chars` / `--max-chars`）挑出目标档位的块；
4. 每个块**连渲 N 次**（同一份参考音）→ 量每次的 cps；
5. 报出该档位的 cps 带宽、波动，以及「按当前阈值会有几个被判成截断」。

⚠️ 渲染时**临时关掉截断校验**（`_assert_not_truncated` 置空）：
   否则超阈值的块直接抛错 —— 恰恰量不到那些「被判截断」的样本。
   校验本身不受影响，这里只是测量。

## 实测结论

⚠️ **两天的结论不一样，因为模型换了** —— 这正是「阈值必须重新标定」的实证。

### ① 2026-10-25（当前：Qwen3-TTS-12Hz-1.7B-**CustomVoice** + 克隆）

生产真实分块 + 生产克隆段，同一上游同一日：

    250 字克隆块（n=20）   5.15 ~ 9.65    中位 ~7.0
    700~900 字块（n=8）    5.43 ~ 6.51
    909~983 字块（n=6）    5.56 ~ 6.49
    短块 40~60 字          6.56 ~ 9.40

⇒ 正常上界 **9.65** ⇒ 阈值取 **11.0**（留 14% 余量）。

⚠️ 老阈值 6.8 ⇒ 生产近 24h 误判 **107 次**「分块被截断，二分重来」
（块长 239~250 字，即**正常块**），每次自愈都凭空多出 1~2 个音色接缝。

⚠️ **没有按块长分档**：实测 cps 与块长**无关**（250 字块能到 9.65，
909 字块只有 6.49 —— 差别来自**文本内容**）。分档只会凭空造出结构。

### ② 2026-10-24（当时：Qwen3-TTS-12Hz-1.7B-**Base** + 克隆）

量法有两种，结果不同 —— **必须用生产真实分块**：

    块长          天然 cps 带宽        波动      6.8 阈值   8.0 阈值
    40~150 字     4.80 ~ 6.12          ≤19.9%      ✓ 有余量   ✓
   182~199 字     5.35 ~ 6.45          ≤10.2%      ✓ 有余量   ✓
      250 字      5.65 ~ 6.25           4.5%      ✓ 有余量   ✓

    真截断样本（生产日志）：7.14 / 7.29 / 7.50 / 8.00 / 8.67

⇒ 那时的自然上界 6.45、真截断下界 7.14 ⇒ 阈值 6.8。

⚠️ **那些「真截断」样本今天在正常带内**（今天正常上界 9.65 > 8.67）
⇒ 它们在当前上游上**无法再与正常区分**。速率判据只能筛「缺得很狠」的截断
   （缺 45% 以上），轻微截尾抓不到 —— 这是**已知能力边界**。

### ③ 同文连渲（同一段文本重复渲染）—— 只用来量**同文波动**，不能定阈值

    47 字  5.88 ~ 6.71        14.3%
    52 字  5.20 ~ 6.50        25.0%
    55 字  5.50 ~ 6.88        25.0%
   114 字  5.70 ~ 6.71        17.6%
   250 字  5.72 ~ 6.25         4.5%

⚠️ **别用这一份定阈值**：它把「同一段文本」重复渲染，测的是**渲染随机性**，
   而自然带是**不同文本**之间的差异 —— 后者更宽（尾部留白、句读停顿占比更大）。
   拿 ③ 定阈值会**定得过紧**。

### ⚠️ 取数时别踩这个坑（实测差异 6.93 vs 6.45）

若把稿子按换行切开再取块，量到的就是**碎片** —— 碎片尾部没有句号与停顿，
读得偏快 → **cps 虚高**（实测同一档位：碎片 6.93，真实分块 6.45），
拿它定阈值会把阈值**定得过紧**。本脚本已在 SQL 里清洗换行，
并复用生产同一条 `split_into_chunks` 逻辑。

## 改阈值前

先量自己这一档，别直接抄上面的数字（**上游换版本 / 换 provider / 换模型**都会变）：

    python scripts/calibrate_tts_chunk_rate.py --chunk-chars 800 --min-chars 700 --max-chars 900

## 用法

    # 生产上量（会打上游，请挑低峰）
    set -a; . /etc/stashbox/stashbox.env; set +a
    cd /opt/stashbox/stashbox/backend
    PYTHONPATH=/opt/stashbox /opt/stashbox/venv/bin/python \
        scripts/calibrate_tts_chunk_rate.py --min-chars 150 --max-chars 260

    # 本机量（需要一个可用的音色库默认音色 + 上游可达）
    PYTHONPATH=<仓库父目录> .venv/bin/python scripts/calibrate_tts_chunk_rate.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
from pathlib import Path

# ── 包路径自举（代码里全是 `stashbox.backend.*` 绝对导入）───────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

# 克隆路径的块长（与 openai.py 的 _CLONE_CHUNK_CHARS_DEFAULT 同源）
CLONE_CHUNK_CHARS = 250


def _rows(sql: str) -> list[list[str]]:
    """查库。

    ⚠️ 用 `--csv` 而不是 `-F '\\t'`：文章标题里可能有换行/制表符
    → 按分隔符切会把一行拆成两行。CSV 有正经的引号转义。
    ⚠️ psql 的 `--csv` 默认分隔符是 **|**（历史默认），不是逗号 → 必须显式指定。
    """
    import csv
    import io

    env = dict(os.environ, PGPASSWORD=os.getenv("POSTGRES_PASSWORD", ""))
    out = subprocess.run(
        [
            "psql",
            "-h",
            os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "-U",
            os.getenv("POSTGRES_USER", "stashbox"),
            "-d",
            os.getenv("POSTGRES_DB", "stashbox"),
            "--csv",
            "--field-separator=,",
            "-A",
            "-t",
            "-c",
            sql,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [r for r in csv.reader(io.StringIO(out)) if r]


def _pick_chunks(
    scripts: list[str], lo: int, hi: int, want: int, chunk_chars: int = CLONE_CHUNK_CHARS
) -> list[str]:
    """按字数区间挑块：优先覆盖区间两端的样本。

    ⚠️ 导入放在函数内：`app.services.tts` 的包初始化会连带执行
    `common.config`（pydantic Settings 校验 JWT_SECRET）——
    在模块级导入会让「只想用这个脚本量一下」的人被环境校验挡住
    （与 ai-service/agent/tools.py 里的同款约束一致）。
    """
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks

    pool = [c for s in scripts for c in split_into_chunks(s, chunk_chars) if lo <= len(c) <= hi]
    pool.sort(key=len)
    if not pool:
        return []
    half = max(1, want // 2)
    picked = pool[:half] + [c for c in pool if c not in pool[:half]][: want - half]
    return picked


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-chars", type=int, default=40, help="目标档位下界（字）")
    ap.add_argument("--max-chars", type=int, default=150, help="目标档位上界（字）")
    ap.add_argument("--sample", type=int, default=8, help="取多少个块做样本")
    ap.add_argument("--repeat", type=int, default=3, help="每个块连渲几次（量波动）")
    ap.add_argument("--articles", type=int, default=10, help="取多少篇真实稿件")
    ap.add_argument(
        "--chunk-chars",
        type=int,
        default=CLONE_CHUNK_CHARS,
        help=(
            "用多大的块长切样本（默认=现网克隆块长）。"
            "**要标定新块长的阈值时必须传它** —— 否则切出来的还是旧块长，"
            "在新区间里一个样本都挑不到。"
        ),
    )
    ap.add_argument(
        "--thresholds",
        default="6.8,8.0",
        help="要检查的阈值（逗号分隔）—— 报出按这些阈值会有几个被判成截断",
    )
    args = ap.parse_args()

    from stashbox.backend.app.services.tts import openai as openai_module
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_duration import audio_duration_exact_sec
    from check_tts_voice_consistency import _resolve_voice

    rows = _rows(
        # ⚠️ **必须把换行清掉**：psql 的 `--csv` **不给内嵌换行加引号**
        # → 一篇稿会被拆成多行 → 拿到的不是「稿子」而是「被换行切开的碎片」。
        # 碎片尾部没有句号/停顿，读得偏快 → **cps 偏高**，量出来的带宽是失真的
        # （实测：同一档位，碎片量到 6.93 字/秒，生产真实分块量到 5.7~6.25）。
        # 清洗后再取，量到的才是生产同一条切块逻辑会切出来的块。
        "select replace(replace(script_text, chr(10), ' '), chr(13), ' ') "
        "from distilled_articles "
        "where script_text is not null and length(script_text) > 300 "
        "order by article_id limit %d" % args.articles
    )
    scripts = [r[0] for r in rows]
    if not scripts:
        print("库里没有可用的稿件（script_text）—— 换个环境或先跑几篇蒸馏")
        return 2
    print(f"真实稿件 {len(scripts)} 篇")

    picked = _pick_chunks(scripts, args.min_chars, args.max_chars, args.sample, args.chunk_chars)
    if not picked:
        print(
            f"在 {args.min_chars}~{args.max_chars} 字区间里没挑到块 —— "
            f"换个区间试试（--min-chars/--max-chars）"
        )
        return 2
    print(
        f"目标档位 {args.min_chars}~{args.max_chars} 字（切块粒度 {args.chunk_chars}），"
        f"样本 {len(picked)} 个：{sorted(len(c) for c in picked)}"
    )

    ref_audio, ref_text, voice_name = await _resolve_voice()
    print(f"克隆音源：{voice_name}\n")

    client = await tts_reload()
    print(f"provider={client.provider_name} model={getattr(client, 'model', '-')}")

    # 临时关掉截断校验（只影响本次测量）
    orig_assert = openai_module.OpenAITTSClient._assert_not_truncated
    openai_module.OpenAITTSClient._assert_not_truncated = lambda self, text, audio: None

    all_cps: list[float] = []
    try:
        for text in picked:
            cpss: list[float] = []
            for _ in range(args.repeat):
                audio = await client.synthesize(
                    text,
                    voice=getattr(client, "voice", None),
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                )
                dur = audio_duration_exact_sec(audio)
                if not dur:
                    continue
                cpss.append(len(text) / dur)
            if not cpss:
                continue
            all_cps.extend(cpss)
            spread = (max(cpss) - min(cpss)) / min(cpss) * 100
            print(f"  {len(text):3d}字  cps {min(cpss):5.2f}~{max(cpss):5.2f}  波动 {spread:4.1f}%")
    finally:
        openai_module.OpenAITTSClient._assert_not_truncated = orig_assert

    if not all_cps:
        print("没测到任何样本 —— 上游不可达？")
        return 2

    print(
        f"\ncps 汇总：n={len(all_cps)} 最小 {min(all_cps):.2f} "
        f"中位 {statistics.median(all_cps):.2f} 最大 {max(all_cps):.2f}"
    )
    for thr in [float(x) for x in args.thresholds.split(",") if x.strip()]:
        over = sum(1 for c in all_cps if c > thr)
        verdict = "会误伤正常渲染" if over else "这一档有余量"
        print(f"  阈值 {thr}: {over}/{len(all_cps)} 会被判成截断（{verdict}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
