#!/usr/bin/env python3
"""clean_impact.py —— 存量音色影响面的**权威干净重算**（可复算）。

## 为什么 previous join 作废
拉全量音频(18:20)时首批已有 5 篇重生成完成，DB audio_url 指向新文件 ⇒
旧版桶被新音频稀释。本脚本按**文件 mtime 身份**归位，不按 DB 当前指向：
  OLD = 本批次开始前(mtime < BATCH_START)的最新旧版；或孤儿目录旧版(2篇)
  NEW = 本批次产物(mtime >= BATCH_START)
  不可恢复 = 旧文件已随重生成删除、孤儿目录也没有(3篇 V0)

## 判据口径（同基准，先质疑口径）
尺子 = ECAPA 相邻滑窗余弦(6s窗/2s步)，与 calib_sliding.py 标定同单位。
  换人   : min <= ECAPA_DIFF_MAX(0.70)  ← 从仓内 consistency.py import，两单位通用
           滑窗标定：真换人基准 min 0.5487/0.5514，同一人基准 0/227 跌破 0.70 ⇒ 可靠
  同一人 : min >= SLIDING_SAME_MIN(0.82) ← 滑窗单位单独标定(同一人基准最低 0.8227)
           ⚠️ 不用 consistency.py 的 ECAPA_SAME_MIN=0.85 —— 那是「整块vs参考音」单位
  不确定 : 0.70 < min < 0.82
"""

from __future__ import annotations
import importlib.util
import json
import os
import sys
import datetime
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path("/tmp/tts_verify")
ALL = OUT / "all"
BATCH_START = datetime.datetime(2026, 11, 25, 18, 2).timestamp()
BATCH = set(
    """art_1ed13969ad884bd9aafd3164 art_4197eecd6cf9445db3d2243a
art_6bf654c047654ec8bd1d9d64 art_470217e39000450dbe853311 art_fc555b2985924e4f8508d7e3
art_89ff6e0f1d24430f9601bca9 art_248128c873ff4606856152f7 art_59b96bce88b548b0bbaeee4c
art_c660c59176a1447d8dacdbce art_ff3c7960c1574eb49790ec91 art_1b7999b837da40e787aead89
art_f24b649150eb4d14b1ee3722 art_488fc6622e9442849a7be149 art_1a259c1a090e42198d04050a
art_6d0d007edf3b4bc59def36c9 art_13d29ced47c9472c9b734fa3 art_a7f9ed74f5114f3ba9c15236
art_72647b498ef34899b428fcc2""".split()
)
# 旧文件已删、但孤儿目录留有 Oct-22 旧版的 2 篇（result.json 前后对照用的就是它们）
ORPHAN_OLD = {
    "art_1ed13969ad884bd9aafd3164": "art_1ed13969ad884bd9aafd3164.d77c3893.mp3",
    "art_4197eecd6cf9445db3d2243a": "art_4197eecd6cf9445db3d2243a.70c5cad7.mp3",
}
VER_LABEL = {
    "V0": "V0 克隆未生效·块250",
    "V1": "V1 p25对齐·块250",
    "V2": "V2 中位对齐·块250",
    "V3": "V3 中位对齐·块800",
    "V4": "V4 本次修复·块1500",
}

# 换人边界从仓内 import（唯一真相源）；滑窗同一人边界用标定值
_spec = importlib.util.spec_from_file_location(
    "_cons", "/Users/hornet/work/stashbox/backend/app/services/tts/consistency.py"
)
cons = importlib.util.module_from_spec(_spec)
sys.modules["_cons"] = cons
_spec.loader.exec_module(cons)
DIFF_MAX = cons.ECAPA_DIFF_MAX  # 0.70
SLIDING_SAME_MIN = 0.82  # 滑窗单位标定（calib_sliding.py），非整块 0.85


def cls(m):
    return "换人" if m <= DIFF_MAX else ("同一人" if m >= SLIDING_SAME_MIN else "不确定")


def load_scans():
    s = {}
    for name in ("all_scan.jsonl", "new_scan.jsonl"):
        p = OUT / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "min" in r:
                s[r["file"]] = r
    return s


def main():
    arts = json.load(open(OUT / "articles.json", encoding="utf-8"))
    scans = load_scans()
    # 权威新文件名 = 生产 DB 当前 audio_url（避免孤儿旧版 mtime≥批次起点被误判成新）
    NEW_URLS = (
        json.load(open(OUT / "new_urls.json", encoding="utf-8"))
        if (OUT / "new_urls.json").exists()
        else {}
    )
    byaid = defaultdict(list)
    for f in os.listdir(ALL):
        if f.endswith(".mp3"):
            byaid[f.split(".")[0]].append((os.path.getmtime(ALL / f), f))

    old_rows, new_rows, unrec = [], [], []
    for a in arts:
        aid = a["id"]
        fs = byaid.get(aid, [])
        olds = sorted([(t, f) for t, f in fs if t < BATCH_START])
        of = olds[-1][1] if olds else ORPHAN_OLD.get(aid)
        if of and of in scans:
            r = dict(scans[of])
            r.update(id=aid, ver=a["ver"], cls=cls(r["min"]), created_at=a["created_at"], file=of)
            old_rows.append(r)
        elif aid in BATCH:
            unrec.append(aid)
        nf = NEW_URLS.get(aid)
        if not nf:
            news = sorted(
                [(t, f) for t, f in fs if t >= BATCH_START and f not in ORPHAN_OLD.values()]
            )
            nf = news[-1][1] if news else None
        if nf and nf in scans:
            r = dict(scans[nf])
            r.update(id=aid, ver="V4", cls=cls(r["min"]), file=nf)
            new_rows.append(r)

    print("=" * 96)
    print(f"【旧音频·按流水线版本】滑窗判据 换人≤{DIFF_MAX} / 同一人≥{SLIDING_SAME_MIN}")
    print(f"{'版本':<24}{'篇数':>5}{'换人':>6}{'不确定':>7}{'同一人':>7}{'最低':>9}{'中位':>9}")
    byv = defaultdict(list)
    for r in old_rows:
        byv[r["ver"]].append(r)
    for v in sorted(byv):
        rs = byv[v]
        c = Counter(x["cls"] for x in rs)
        ms = sorted(x["min"] for x in rs)
        print(
            f"{VER_LABEL[v]:<24}{len(rs):>5}{c['换人']:>6}{c['不确定']:>7}{c['同一人']:>7}"
            f"{ms[0]:>9.4f}{ms[len(ms)//2]:>9.4f}"
        )
    c = Counter(x["cls"] for x in old_rows)
    print(f"{'合计(可复核)':<24}{len(old_rows):>5}{c['换人']:>6}{c['不确定']:>7}{c['同一人']:>7}")
    print(f"旧音频不可恢复(已随首批删除): {len(unrec)} 篇 {unrec}")

    bad = [r for r in old_rows if r["cls"] == "换人"]
    inb = [r for r in bad if r["id"] in BATCH]
    outb = [r for r in bad if r["id"] not in BATCH]
    print(
        f"\n【影响面】确证真换人 {len(bad)} 篇 / 可复核 {len(old_rows)} 篇"
        f"（另 {len(unrec)} 篇 V0 不可复核）"
    )
    print(f"  首批18篇已覆盖其中 {len(inb)} 篇；仍需处理 {len(outb)} 篇")

    print("\n【重生成前后对照】同篇同单位")
    nm = {r["id"]: r for r in new_rows}
    print(f"{'文章':<30}{'旧ver':>6}{'旧min':>9}{'旧判':>8}{'新min':>9}{'新判':>8}")
    for r in sorted(old_rows, key=lambda x: x["min"]):
        if r["id"] in nm:
            n = nm[r["id"]]
            print(
                f"{r['id']:<30}{r['ver']:>6}{r['min']:>9.4f}{r['cls']:>8}"
                f"{n['min']:>9.4f}{n['cls']:>8}"
            )
    if new_rows:
        cn = Counter(r["cls"] for r in new_rows)
        mn = [r["min"] for r in new_rows]
        print(
            f"\n【新音频汇总】{len(new_rows)} 篇  换人{cn['换人']} / 不确定{cn['不确定']} / 同一人{cn['同一人']}"
            f"  最低 {min(mn):.4f} 中位 {sorted(mn)[len(mn)//2]:.4f}"
        )

    print(f"\n【仍需处理的 {len(outb)} 篇确证换人】")
    for r in sorted(outb, key=lambda x: x["min"]):
        print(
            f"  {r['id']}  {r['ver']}  min={r['min']:.4f} @{r['min_at_s']}s"
            f"  真换人对={r['n_below_diff']}"
        )

    json.dump(
        {
            "old": old_rows,
            "new": new_rows,
            "unrecoverable": unrec,
            "remaining_bad": [r["id"] for r in outb],
        },
        open(OUT / "clean_impact.json", "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=1,
    )
    print("\n→ /tmp/tts_verify/clean_impact.json")


if __name__ == "__main__":
    main()
