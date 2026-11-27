#!/usr/bin/env python3
"""exp_tts_preset_stability_judge.py —— 预设音色稳定性实验的判定端（本机离线）。

配套采集端 `exp_tts_preset_stability.py`（生产实跑）。
生产 venv 没有 torch/speechbrain ⇒ 素材 rsync 回本机判定。

## 预注册判据（预设 = 固定说话人）

1. **整段全对余弦**：所有 P 块两两 ≥ ECAPA_SAME_MIN；任何一对 ≤ ECAPA_DIFF_MAX ⇒ 换人，否证。
2. **接缝 ±4s 窗口对**（预设正确口径，见下）：每条缝取「窗口对中点落在接缝时刻 ±4s」
   的那些对，其 min ≥ SW_SAME(0.82) 且无一对 ≤ SW_DIFF(0.70)；同时整段 ≥ SAME_MIN。
3. 同文重渲（R1/R2 vs P01）噪声底参照。

## ⚠️ 为什么判据2 不用「拼接件全局滑窗 min」（2026-11-25 取证修正）

最初预注册的判据2 是「相邻块硬拼后**整条**滑窗 min ≥ 0.82」。那条刻度标定于
**克隆素材**（同人带紧：单块自比 min 0.8227）。但**预设素材的同人变异更宽**：
单块自比 min 低到 **0.698**（P05，块内部、距接缝 -101.5s 处）。取证表明这些 dips：

* 全部落在**块内部**（单块自比就有，与接缝无关）；
* 是**孤立单窗口对**，前后立即恢复 0.93/0.97（真换人会连续多对压低 + 整段余弦掉）；
* 真正的接缝区域（±4s）窗口对**全部 ≥ 0.8519**，P05↔P06 整段 = 0.9686。

⇒ 「全局滑窗 min」把**块内自然韵律变异**误判成接缝换人 ⇒ 对预设素材**口径误设**。
正确判据 = 把窗口对**定位到接缝时刻 ±4s** + 整段身份。全局 min 与单块自比包络
仍打印出来作**诊断参照**（说明误设来源），但**不作门禁**。

阈值唯一真相源 = 仓内 consistency.py（整段单位）；滑窗 0.82/0.70 是契约 §2.4.1
单独标定的滑窗单位刻度。接缝 ±4s 用滑窗刻度（6s 窗），整段用整段刻度。

## 用法

    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
        /tmp/tts_analysis/ecapa_venv/bin/python exp_tts_preset_stability_judge.py \
        --dir /tmp/tts_preset_stability

退出码：0 = 判据全过；1 = 有判据失败；2 = 环境/素材问题。
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_CONS = Path(__file__).resolve().parents[1] / "app" / "services" / "tts" / "consistency.py"
_spec = importlib.util.spec_from_file_location("_stashbox_consistency", _CONS)
_cons = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _cons
_spec.loader.exec_module(_cons)
SAME_MIN = _cons.ECAPA_SAME_MIN
DIFF_MAX = _cons.ECAPA_DIFF_MAX

# 滑窗单位刻度（契约 §2.4.1，已知真值单独标定）
SW_SAME = 0.82
SW_DIFF = 0.70
WIN_S = 6.0
HOP_S = 2.0
SEAM_HALF_S = 4.0  # 接缝取证窗口：中点落在接缝 ±4s 的窗口对


def _load_embed():
    import numpy as np
    import torch
    from speechbrain.inference.speaker import EncoderClassifier

    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="/tmp/tts_analysis/ecapa_model",
        run_opts={"device": "cpu"},
    )

    def embed(y):
        with torch.no_grad():
            w = torch.from_numpy(np.asarray(y, dtype="float32")).unsqueeze(0)
            e = model.encode_batch(w).reshape(-1).numpy()
        n = float(np.linalg.norm(e))
        return e / (n if n else 1.0)

    return embed


def _read(path):
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, rate


def _cos(a, b):
    import numpy as np

    return float(np.dot(a, b))


def _sliding_pairs(embed, y, rate):
    """滑窗相邻余弦对列表 [(cos, 窗口对中点时刻s)]（6s 窗 2s 步）。"""
    win = int(WIN_S * rate)
    hop = int(HOP_S * rate)
    starts = list(range(0, max(len(y) - win, 0) + 1, hop))
    if len(starts) < 2:
        return []
    es = [embed(y[s : s + win]) for s in starts]
    return [
        (_cos(es[i], es[i + 1]), (starts[i] + starts[i + 1]) / 2 / rate) for i in range(len(es) - 1)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    root = Path(args.dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    blocks = [b for b in manifest["blocks"] if not b.get("error")]
    if len(blocks) < 3:
        print("❌ 素材不足")
        return 2
    print(f"voice={manifest['voice']} model={manifest['model']} 块数={len(blocks)}")

    embed = _load_embed()
    import numpy as np

    audio = {}
    for b in blocks:
        y, rate = _read(root / f"{b['label']}.mp3")
        audio[b["label"]] = (y, rate)

    # ── 判据 1：整段全对 ─────────────────────────────────────────────
    es = {lab: embed(y) for lab, (y, _) in audio.items()}
    p_labels = [b["label"] for b in blocks if b["label"].startswith("P")]
    pairs = []
    for a, b in itertools.combinations(p_labels, 2):
        pairs.append((a, b, _cos(es[a], es[b])))
    vals = [v for _, _, v in pairs]
    worst = min(pairs, key=lambda t: t[2])
    n_diff = sum(1 for v in vals if v <= DIFF_MAX)
    n_below_same = sum(1 for v in vals if v < SAME_MIN)
    print(
        f"\n▶ 判据1 整段全对（{len(pairs)} 对）：min={min(vals):.4f} med={float(np.median(vals)):.4f} max={max(vals):.4f}"
    )
    print(f"  ≤{DIFF_MAX}（换人）: {n_diff} 对；<{SAME_MIN}（未达同人带）: {n_below_same} 对")
    print(f"  最低对: {worst[0]}↔{worst[1]} = {worst[2]:.4f}")
    ok1 = n_diff == 0 and n_below_same == 0

    # ── 判据 2：接缝 ±4s 窗口对（预设正确口径）─────────────────────
    print(f"\n▶ 判据2 接缝 ±{SEAM_HALF_S:.0f}s 窗口对（{WIN_S}s/{HOP_S}s，相邻硬拼；预设正确口径）")
    ok2 = True
    for a, b in zip(p_labels, p_labels[1:]):
        ya, ra = audio[a]
        yb, rb = audio[b]
        assert ra == rb
        joined = np.concatenate([ya, yb])
        junction = len(ya) / ra  # 接缝时刻 = 前块时长
        sp = _sliding_pairs(embed, joined, ra)
        near = [v for v, t in sp if abs(t - junction) <= SEAM_HALF_S]
        whole = _cos(es[a], es[b])
        if not near:
            print(f"  {a}↔{b}: ⚠️ 接缝 ±4s 无窗口对（块太短）")
            ok2 = False
            continue
        seam_min = min(near)
        n_diff_seam = sum(1 for v in near if v <= SW_DIFF)
        flag = "真换人" if n_diff_seam else ("同一人" if seam_min >= SW_SAME else "不确定")
        if seam_min < SW_SAME or n_diff_seam or whole < SAME_MIN:
            ok2 = False
        print(f"  {a}↔{b}: 整段={whole:.4f}  缝±4s min={seam_min:.4f}（{len(near)} 对）→ {flag}")

    # ── 诊断参照：全局滑窗 min（克隆刻度，对预设口径误设）+ 单块自比包络 ──
    print("\n▶ 诊断参照（不作门禁）：全局滑窗 min 标定于克隆素材，对预设偏严")
    for a, b in zip(p_labels, p_labels[1:]):
        ya, ra = audio[a]
        yb, _ = audio[b]
        joined = np.concatenate([ya, yb])
        sp = _sliding_pairs(embed, joined, ra)
        if sp:
            gmin, tm = min(sp, key=lambda t: t[0])
            junction = len(ya) / ra
            print(
                f"  {a}↔{b}: 全局min={gmin:.4f}@{tm:.1f}s（接缝{junction:.1f}s，偏移{tm-junction:+.1f}s）"
            )
    print("▶ 诊断参照 单块自比滑窗 min（自然变异包络 —— 证明块内 dips 与接缝无关）")
    self_mins = []
    for lab in p_labels:
        y, rate = audio[lab]
        sp = _sliding_pairs(embed, y, rate)
        if sp:
            m = min(v for v, _ in sp)
            self_mins.append(m)
            print(f"  {lab}: 自比min={m:.4f}")
    if self_mins:
        print(
            f"  自比包络: {min(self_mins):.4f} ~ {max(self_mins):.4f}（克隆素材单块自比下界 0.8227，预设更宽）"
        )

    # ── 参照：同文重渲噪声底 ───────────────────────────────────────
    print("\n▶ 参照 同文重渲（R1/R2 vs P01，整段）")
    for r in ("R1", "R2"):
        if r in es and "P01" in es:
            print(f"  {r}↔P01: {_cos(es[r], es['P01']):.4f}")
    if "R1" in es and "R2" in es:
        print(f"  R1↔R2: {_cos(es['R1'], es['R2']):.4f}")

    print(
        f"\n═══ 结论：判据1 {'过' if ok1 else '不过'} / 判据2(缝±4s) {'过' if ok2 else '不过'} ═══"
    )
    print("（对照：克隆路径生产接缝失败率 ≈ 12%；预设整段全对 + 缝±4s 全过 ⇒ 固定说话人成立）")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
