#!/usr/bin/env python3
"""check_tts_timbre_ecapa.py — 音色**身份**的离线金标准审计（ECAPA-TDNN）。

## 这个脚本回答什么

「一段音频和另一段音频**是不是同一个人**」—— 这是音色一致性的**身份层**，
与音高、响度、接缝都无关。

## 为什么必须用 ECAPA，而不是自己写一个 MFCC 距离

2026-10-25 实测（**否证过程**，结论已写进仓）：

    自写零依赖 MFCC 余弦（当时叫 app/services/tts/timbre.py）
        · 同一信号的窗间距离 0.32 **大于**分段间 0.16 ⇒ 被**内容**主导
        · 与 ECAPA 金标准的 Spearman ρ = **0.078**（87 对样本）⇒ 几乎不相关
    ⇒ 该模块**不应**以「判据」身份进仓（已删除）。

教训（也写进了 `app/services/tts/consistency.py`）：
**度量本身要先校准** —— 坏统计量会把修复指向反方向。

## 为什么它**不能**在生产运行时跑

ECAPA 需要 `torch` + `speechbrain`（模型 ~80MB），生产 venv 只有 numpy/ffmpeg。
⇒ 它是**离线审计**工具：在有 torch 的机器上跑，用来标定/复核分离带，
以及抽查生产产出的音频。生产运行时的门禁在 `consistency.py` 的电平层与段数层。

## 标定结果（2026-10-25，同一上游 tertu.cn / Qwen3-TTS-12Hz-1.7B-CustomVoice）

    同一人（克隆段 / 同音色另一次渲染）  整段余弦 0.75 ~ 0.80
    同一段文本重渲一次                   整段余弦 0.93 ~ 0.95
    换人（预设音色 vivian / serena）      整段余弦 0.29 ~ 0.35

⇒ 分离带：换人 ≤ 0.35，同一人 ≥ 0.75；门禁阈值 0.55（`consistency.ECAPA_THRESHOLD`）。

⚠️ **绝对阈值依实现而变**（F5-TTS 模型卡原话）—— 换模型/换上游/换 ECAPA 版本
必须**重新标定**，别把这里的数抄到别处去。

## 用法

    # 审计一批分段（与参考音比）
    python scripts/check_tts_timbre_ecapa.py --ref ref.mp3 --segments a.mp3 b.mp3 c.mp3

    # 审计某个目录里的分段（按文件名排序）
    python scripts/check_tts_timbre_ecapa.py --ref ref.mp3 --dir /tmp/segments

    # 自检：确认嵌入管线本身是通的（合成信号，同信号应≈1，音 vs 噪应远小于 1）
    python scripts/check_tts_timbre_ecapa.py --self-test

退出码：0 = 全部同一人；1 = 发现疑似换人；2 = 跑不起来（缺依赖/文件缺失）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ── 包路径自举（代码里是 `stashbox.backend.*` 绝对导入）──────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from stashbox.backend.app.services.tts.consistency import (  # noqa: E402
    ECAPA_DIFF_MAX,
    ECAPA_SAME_MIN,
    ECAPA_THRESHOLD,
)


def _load_model():
    """加载 ECAPA-TDNN（speechbrain）。返回 (model, embed_fn)。"""
    try:
        import numpy as np
        import torch
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise SystemExit(
            f"❌ 缺少依赖：{exc}\n"
            f"   ECAPA 审计需要 torch + speechbrain（生产 venv 没有，这是**离线工具**）。\n"
            f"   安装示例：pip install torch speechbrain soundfile"
        ) from exc

    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(_REPO_ROOT / ".cache" / "ecapa"),
        run_opts={"device": "cpu"},
    )

    def embed(y):
        with torch.no_grad():
            w = torch.from_numpy(np.asarray(y, dtype="float32")).unsqueeze(0)
            e = model.encode_batch(w).reshape(-1).numpy()
        norm = float(np.linalg.norm(e))
        return e / (norm if norm else 1.0)

    return embed


def _read_audio(path: Path):
    import soundfile as sf

    data, _ = sf.read(str(path), dtype="float32")
    return data if data.ndim == 1 else data.mean(axis=1)


def _cos(a, b) -> float:
    import numpy as np

    return float(np.dot(a, b))


def _self_test(embed) -> int:
    """合成信号自检：确认嵌入管线本身是通的。"""
    import numpy as np

    rate = 16000
    t = np.arange(rate * 3) / rate
    tone = np.sin(2 * np.pi * 180 * t).astype("float32")
    noise = (np.random.default_rng(0).normal(0, 0.1, size=len(t))).astype("float32")

    same = _cos(embed(tone), embed(tone))
    diff = _cos(embed(tone), embed(noise))
    print(f"自检：同信号余弦 {same:.4f}（应 ≈1.0）；音 vs 噪 {diff:.4f}（应远小于 1）")
    ok = same > 0.9 and diff < same - 0.2
    print("✅ 自检通过" if ok else "❌ 自检不通过 —— 别拿这个管线下的数当结论")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="音色身份（ECAPA）离线审计")
    ap.add_argument("--ref", help="参考音频（克隆源）")
    ap.add_argument("--segments", nargs="*", default=[], help="待审计的分段音频")
    ap.add_argument("--dir", help="待审计的分段目录（按文件名排序）")
    ap.add_argument("--self-test", action="store_true", help="只做管线自检")
    args = ap.parse_args()

    try:
        embed = _load_model()
    except SystemExit as exc:
        print(exc)
        return 2

    if args.self_test:
        return _self_test(embed)

    if not args.ref:
        print("❌ 需要 --ref（参考音频）")
        return 2

    segments: list[Path] = [Path(p) for p in args.segments]
    if args.dir:
        segments += sorted(Path(args.dir).glob("*.mp3"))
        segments += sorted(Path(args.dir).glob("*.bin"))
        segments += sorted(Path(args.dir).glob("*.wav"))
    if not segments:
        print("❌ 需要 --segments 或 --dir")
        return 2

    ref = embed(_read_audio(Path(args.ref)))
    print(f"参考音: {args.ref}")
    print(
        f"分离带（标定于 2026-10-25）：同一人 ≥ {ECAPA_SAME_MIN}，"
        f"换人 ≤ {ECAPA_DIFF_MAX}，门禁阈值 {ECAPA_THRESHOLD}\n"
    )

    embs = []
    for path in segments:
        try:
            e = embed(_read_audio(path))
        except Exception as exc:  # noqa: BLE001
            print(f"  {path.name:<28} 读取失败：{exc}")
            continue
        embs.append((path.name, e))

    bad = []
    print(f"{'分段':<30}{'vs 参考音':>12}   判定")
    for name, e in embs:
        score = _cos(e, ref)
        if score >= ECAPA_SAME_MIN:
            verdict = "同一人"
        elif score <= ECAPA_DIFF_MAX:
            verdict = "**换人**"
            bad.append((name, score))
        else:
            verdict = "**不确定**（落在分离带内）"
            bad.append((name, score))
        print(f"{name:<30}{score:>12.3f}   {verdict}")

    if len(embs) >= 2:
        print("\n分段两两之间：")
        for i, (n1, e1) in enumerate(embs):
            for n2, e2 in embs[i + 1 :]:
                print(f"  {n1} ↔ {n2}: {_cos(e1, e2):.3f}")

    if bad:
        print(f"\n❌ {len(bad)} 个分段没落在「同一人」侧：{bad}")
        return 1
    print("\n✅ 全部分段都在「同一人」侧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
