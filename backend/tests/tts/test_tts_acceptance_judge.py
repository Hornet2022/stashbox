"""验收脚本的块级判据守卫（2026-11-27）。

钉住一次**真实假红**：art_67f7fa22ef5c419680748072 的 chunk1。

## 假红是怎么来的

老判据（已废弃）：块时长 = 上游响应字节 ÷ **反推码率**，
而反推码率 = 上游总字节 ÷ 整篇实测总时长。

**前提是错的**：最终 mp3 是 `crossfade_concat` **重编码**（libmp3lame 64k）
的产物，字节数与上游各块之和对不上（实测 4,774,509 vs 4,208,592，差 13.4%）
⇒ 反推出的块时长系统性偏短 ⇒ 真实 5.997 字/秒的块被算成 6.21 ⇒ **假红**。

⇒ 新判据只认一个权威来源：服务端对**上游返回 mp3** 的逐帧精测时长
  （`audio_duration_exact_sec`，落在日志的 `OpenAITTS 块校验:` 行）。

## 这里钉死什么

  · 两种判据在**同一份真实生产数字**上的结论必须相反（反推判红、精测判过），
    且**假红的那一侧不得再被任何测试当成"正确行为"**。
  · 新判据的 fail-closed 行为：无精测记录 / 字节不吻合 / 阈值对账不一致
    ⇒ 一律判失败，不许"看起来差不多就放过"。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "exp_live_acceptance_tts_verify.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_accept_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 真实生产数字（art_67f7fa22ef5c419680748072，2026-11-27）──────────────
_ARTICLE = {
    "total_upstream_bytes": 4208592,  # 5 块上游响应字节之和
    "actual_total_sec": 596.72,  # ffprobe 实测整篇时长
    "final_mp3_bytes": 4774509,  # 最终文件字节（≠ 上游之和 ⇒ 重编码）
}
# chunk1：667 字 / 请求 1503 B / 上游响应 758064 B
_CHUNK1 = {
    "chars": 667,
    "req_bytes": 1503,
    "resp_bytes": 758064,
    "exact_dur_sec": 111.192,  # 服务端逐帧精测
    "exact_cps": 5.998,  # 667 / 111.192
}


def test_反推码率的前提已被证伪():
    """最终文件字节 ≠ 上游各块之和 ⇒ 反推码率这条路在重编码管线下失效。

    这是整件事的根：**尺子的前提不成立**，不是某个块特别可疑。
    """
    assert (
        _ARTICLE["final_mp3_bytes"] != _ARTICLE["total_upstream_bytes"]
    ), "若最终文件恰好等于上游字节之和，本测试的前提就变了 ⇒ 需要重新评估判据"
    ratio = _ARTICLE["final_mp3_bytes"] / _ARTICLE["total_upstream_bytes"]
    assert ratio > 1.05, f"实测比例 {ratio:.4f}（预期重编码后明显变大）"


def test_两种判据在真实样本上结论相反():
    """反推判据 ⇒ 6.21（假红）；服务端精测判据 ⇒ 5.998（通过）。"""
    upstream_bps = _ARTICLE["total_upstream_bytes"] / _ARTICLE["actual_total_sec"]
    derived_dur = _CHUNK1["resp_bytes"] / upstream_bps
    derived_cps = _CHUNK1["chars"] / derived_dur
    assert derived_cps > 6.0, (
        f"反推判据在这份真实样本上必须判红（实测 {derived_cps:.3f}）——"
        "若不再判红，说明本测试样本已不代表当年的误判现场"
    )
    assert _CHUNK1["exact_cps"] <= 6.0, f"服务端精测值必须通过（实测 {_CHUNK1['exact_cps']}）"


def test_新判据对真实样本判通过():
    """用服务端精测值 + 仓内阈值 ⇒ 这一块必须判 OK。"""
    mod = _load_script()
    chunk = "这" * _CHUNK1["chars"]
    req = len(chunk.encode("utf-8"))
    rows, bad = mod.judge_chunks(
        [chunk],
        [(req, _CHUNK1["resp_bytes"])],
        [
            {
                "chars": _CHUNK1["chars"],
                "dur": _CHUNK1["exact_dur_sec"],
                "cps": _CHUNK1["exact_cps"],
                "limit": 6.0,
            }
        ],
        strict=True,
    )
    assert not bad, f"真实样本不得判失败：{rows}"
    assert rows[0]["verdict"] == "OK"


def test_无精测记录必须判不可判定():
    """没有服务端精测值 ⇒ **不可判定**，整体判失败（fail-closed）。"""
    mod = _load_script()
    chunk = "这" * 667
    req = len(chunk.encode("utf-8"))
    rows, bad = mod.judge_chunks(
        [chunk],
        [(req, 758064)],
        [],  # ← 服务端没留精测记录
        strict=True,
    )
    assert bad, "缺精测记录必须判失败，不许默认通过"
    assert "不可判定" in rows[0]["verdict"]


def test_字节不吻合必须判失败():
    """块↔日志的 UTF-8 字节对不上 ⇒ 映射错位 ⇒ 后面判定全不可信。"""
    mod = _load_script()
    chunk = "这" * 667
    req = len(chunk.encode("utf-8"))
    rows, bad = mod.judge_chunks(
        [chunk],
        [(req + 7, 758064)],  # ← 日志里的请求字节对不上
        [
            {
                "chars": 667,
                "dur": _CHUNK1["exact_dur_sec"],
                "cps": _CHUNK1["exact_cps"],
                "limit": 6.0,
            }
        ],
        strict=True,
    )
    assert bad, "字节不吻合必须判失败"
    assert rows[0]["ok_bytes"] is False


def test_阈值对账不一致必须判失败():
    """服务端日志里的 limit 与仓内阈值不一致 ⇒ 阈值漂了 ⇒ 判失败。"""
    mod = _load_script()
    chunk = "这" * 667
    req = len(chunk.encode("utf-8"))
    rows, bad = mod.judge_chunks(
        [chunk],
        [(req, 758064)],
        [
            {
                "chars": 667,
                "dur": _CHUNK1["exact_dur_sec"],
                "cps": _CHUNK1["exact_cps"],
                "limit": 9.9,  # ← 与仓内 6.0 对不上
            }
        ],
        strict=True,
    )
    assert bad, "阈值对账不一致必须判失败"
    assert rows[0]["agree"] is False


def test_同文本重渲取最后一次精测值():
    """重渲 ⇒ 最后一条才是发出去的那次；前面的失败记录不该拿来做判据。"""
    mod = _load_script()
    chunk = "这" * 667
    req = len(chunk.encode("utf-8"))
    rows, bad = mod.judge_chunks(
        [chunk],
        [(req, 758064)],
        [
            # 第一次被截断（cps 9.44），重渲后正常（cps 5.998）
            {"chars": 667, "dur": 70.6, "cps": 9.46, "limit": 6.0},
            {"chars": 667, "dur": 111.192, "cps": 5.998, "limit": 6.0},
        ],
        strict=True,
    )
    assert not bad, f"重渲成功 ⇒ 应当判通过：{rows}"
    assert rows[0]["cps"] == 5.998
    assert rows[0]["healed"] == 1
    assert "重渲" in rows[0]["verdict"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
