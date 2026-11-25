#!/usr/bin/env python3
"""exp_tts_contextual_continuation.py —— 「上下文续写」能不能治音色分段不一致。

## 为什么有这个脚本（2026-11-25）

内测反馈：**音频有明显「换人感」**。生产实证的因果链是：

    上游每次请求 = 全新独立随机采样（openai.py:125 实测：同句同 seed 连发两次
    PCM 长度差 19%，temperature=0 也不同）
      ⇒ 各段音域随机漂（生产实测跨度 19%）
      ⇒ 事后用 rubberband 整体移调拉平（实测位移 ±1.64 半音）
      ⇒ 移调本身会染色 ⇒ 听感上就是「音色变了」

⇒ 治标 = 缩小移调量；**治本 = 让上游的采样链式锁定**：
   **把上一段（音频 + 它的文本）当下一段的参考音** ——
   上游下一次采样就锚定在上一段的音色上 ⇒ 漂移从「每段独立」变成「随机游走」
   ⇒ 需要移的量骤降 ⇒ 染色消失。

契约文档 §6 把这条记为「上下文续写未实现，段数降到 3 之后收益变小，先不做」。
但用户实测说「换人感仍然明显」⇒ **那个判断需要用数据重新验**。

## 两组对比

| 组 | 块 N 的参考音 |
|---|---|
| **A 现状** | 全部用**同一份**原始参考音（音色库默认音色） |
| **B 上下文续写** | 块1 用原始参考音；块 N+1 用**块 N 的输出音频 + 块 N 的文本** |

⚠️ 块 N 的输出 + **块 N 的文本**是一对自洽的「参考音频 + 参考文本」——
   上游要求两者内容一致（见 openai.py 的 ref_text 说明）。

## 量什么

- **段间音域散布** `(max-min)/min`（各块 p25）—— 越小越一致
  （p25 是「说话人音域下缘」，比中位数稳；中位数会被 F0 上尾带跑）
- **音域对齐的位移量**（`align_pitch` 的 semitones）—— 越小 ⇒ 染色越少
- **各块 p25** 明细

## 用法

    # 在生产跑（需要 DB 里的 LLM/TTS 配置 + 音色库默认音色）
    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_contextual_continuation.py

    # 自定义文案
    python scripts/exp_tts_contextual_continuation.py --text-file /path/to/script.txt

退出码：0 = 实验跑完（数据看 stdout）；2 = 跑不起来（配置/音色库/上游问题）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# ── 包路径自举（代码里是 `stashbox.backend.*` 绝对导入）───────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

# 默认文案：内容不同的多段（拿同一段重复 N 次，块间音高会一模一样 ⇒ 测不出东西）
DEFAULT_TEXT = """
碎片时间到底能不能学得进去，这是个被问烂了的问题。我的回答是能，但要先接受一件事：碎片时间的价值不在于完成一个完整的目标，而在于让大脑持续保持在被唤醒的状态。你在通勤路上听完一篇深度文章，在午休前读三个行业观点，这些东西单独看都很小，但它们共同维持着一种对世界的敏感度。

很多人把碎片时间当成敌人，觉得它是注意力被切碎的罪魁祸首。但真正毁掉深度阅读的，从来不是时间被切碎，而是期待自己在碎片里完成深度阅读。你带着我要读完一章书的心愿挤进三分钟的地铁电梯间，然后责怪自己读不进去——问题不在那三分钟，在那个期待。

真正稀缺的不是注意力，是判断力。一条内容值不值得读，在打开它之后的三十秒内基本就能判断出来。我的做法是先看标题和前三段，如果觉得有料再往下看，觉得只是自我安慰就立刻划走。判断错了的成本，远远低于读完一篇废话的成本——后者要花你二十分钟，还可能留下我今天学到了什么的错觉。

知识的复利也不在单次学得多深，而在不同来源之间的连接。同一件事，你从三个完全不同的角度看过去，理解会变成一个立体的东西，这时候再回头看，你会发现自己的判断已经变了。碎片时间的优势恰恰在这里：它天然逼着你从多个来源取材，而不是在一棵树上吊死。

当然也有反面。碎片输入很容易变成一种被动消费，像刷feed一样，看似接触了很多信息，实际上一条都没进去。判断标准很简单：如果你能用自己的话复述刚才看到的东西，那它就是输入；如果只是觉得好像有道理，那它就只是噪音。

所以我的建议是：把碎片时间当成积木，而不是当成积木的替代品。单独看每一块都很小，但只要搭的方向对，积着积着就会成形。真正决定形状的，不是你每天有多少碎片，而是你有没有一块都没放错位置。
""".strip()


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}Hz"


def _spread(values: list[float | None]) -> float:
    """段间散布 = (max-min)/min（voice_align._spread 的同口径）。"""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return 0.0
    lo = min(vals)
    if lo <= 0:
        return 0.0
    return (max(vals) - lo) / lo


async def _default_voice() -> tuple[str, str, str]:
    """音色库默认音色 → (ref_audio, ref_text, display_name)。"""
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=False)
    default = next((v for v in voices if v.is_default), None)
    if default is None:
        raise SystemExit(
            "音色库没有默认音色 —— 克隆没有音源可用。\n"
            "  生产上先建一条：上传参考音频 + 参考文本，并设为默认。"
        )
    return default.ref_audio_url, default.ref_text, default.display_name


async def _run_group(
    client,
    chunks: list[str],
    ref_audio: str,
    ref_text: str,
    *,
    contextual: bool,
    ref_max_sec: float = 30.0,
) -> dict:
    """跑一组，返回 {p25s, spread, shifts}。

    A 组：每块都用**同一份**原始参考音。
    B 组：块 N+1 用块 N 的**输出音频 + 块 N 的文本**当参考。

    ⚠️ **参考音有长度上限**（2026-11-25 实测）：
       上游报 `Reference audio too long (114.7s). Maximum 30s supported`
       —— 一块 800 字约 120 秒，**整块输出当参考会被拒**。
       ⇒ B 组只取上一块的**前 ref_max_sec 秒**（+ 对应的前段文本）当参考。

    ⚠️ 参考文本必须与参考音频**内容对应**（上游要求两者一致），
       所以文本也要按同一比例截断。
    """
    from stashbox.backend.app.services.tts.openai import _CloneSpec
    from stashbox.backend.app.services.tts.ref_audio import load_ref_audio_data_url

    base = await client._resolve_clone(ref_audio, ref_text)
    if base is None:
        raise SystemExit("拿不到克隆参数（ref_audio/ref_text 不齐）—— 没法做这个实验")

    p25s: list[float | None] = []
    pieces: list[bytes] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="tts_ctx_"))

    prev_audio: bytes | None = None
    prev_text: str | None = None

    for i, chunk in enumerate(chunks, 1):
        if contextual and prev_audio is not None and prev_text:
            # ① 取上一块的前 ref_max_sec 秒（上游硬上限 30s）
            clip, prev_text = _clip_for_ref(prev_audio, prev_text, ref_max_sec)
            fpath = tmpdir / f"ref_{i}.mp3"
            fpath.write_bytes(clip)
            spec = _CloneSpec(
                source=str(fpath),
                data_url=await load_ref_audio_data_url(str(fpath)),
                text=prev_text,
            )
        else:
            spec = base

        audio = await client._synthesize_chunk(chunk, voice=client.voice, clone=spec)
        pieces.append(audio)
        prev_audio, prev_text = audio, chunk

        # 测这块的音域锚点（p25）
        from stashbox.backend.app.services.tts.voice_align import f0_profile

        prof = f0_profile(audio)
        p25s.append(prof.p25)

    # 音域对齐会移多少（染色量）
    from stashbox.backend.app.services.tts.voice_align import align_pitch

    _aligned, details = align_pitch(pieces)
    shifts = [d.get("semitones") for d in details if d.get("semitones") is not None]

    return {
        "p25s": p25s,
        "spread": _spread(p25s),
        "shifts": shifts,
        "max_shift": max((abs(s) for s in shifts), default=0.0),
    }


def _clip_for_ref(audio: bytes, text: str, max_sec: float) -> tuple[bytes, str]:
    """把上一块的输出裁成「前 max_sec 秒 + 对应的前段文本」。

    ⚠️ 文本必须与音频**对应**（上游要求参考文本与参考音频内容一致），
    所以按时长比例截断文本。
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
        fh.write(audio)
        src = fh.name
    out = src + ".clip.mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src, "-t", str(max_sec), out],
            check=True,
        )
        with open(out, "rb") as fh:
            clipped = fh.read()
    finally:
        for p in (src, out):
            try:
                Path(p).unlink()
            except OSError:
                pass

    # 时长比例 → 文本比例
    try:
        from stashbox.backend.app.services.tts.voice_align import _decode_pcm

        samples = _decode_pcm(audio)
        total = len(samples) / 16000.0 if samples else max_sec
    except Exception:  # noqa: BLE001
        total = max_sec
    ratio = min(1.0, max_sec / total) if total > 0 else 1.0
    n_chars = max(20, int(len(text) * ratio))
    return clipped, text[:n_chars]


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks

    text = (
        Path(args.text_file).read_text(encoding="utf-8").strip() if args.text_file else DEFAULT_TEXT
    )

    ref_audio, ref_text, name = await _default_voice()
    print(f"[音色] 音色库默认音色：{name}")

    client = await tts_reload()
    chunk_chars = int(getattr(client, "clone_chunk_chars", 800) or 800)
    print(f"[客户端] provider={client.provider_name} 克隆块长={chunk_chars} 字")

    chunks = split_into_chunks(text, chunk_chars)
    print(f"[分块] {len(text)} 字 → {len(chunks)} 块\n")

    results = {}
    for label, contextual in (
        ("A 现状（同一份参考音逐块）", False),
        ("B 上下文续写（块N的输出当块N+1的参考）", True),
    ):
        print(f"▶ {label}")
        try:
            res = await _run_group(client, chunks, ref_audio, ref_text, contextual=contextual)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 这一组跑不起来：{type(exc).__name__}: {exc}\n")
            continue
        results[label] = res
        print(f"  各块 p25：{' / '.join(_fmt(v) for v in res['p25s'])}")
        print(f"  段间音域散布：{res['spread'] * 100:.1f}%")
        print(
            f"  对齐位移：{' / '.join(f'{s:+.2f}' for s in res['shifts'])} 半音"
            f"（最大 {res['max_shift']:.2f}）\n"
        )

    if len(results) == 2:
        a, b = list(results.values())
        print("—— 结论（看这三个数）——")
        print(f"  段间音域散布：A {a['spread'] * 100:.1f}%  →  B {b['spread'] * 100:.1f}%")
        print(f"  最大对齐位移：A {a['max_shift']:.2f} 半音  →  B {b['max_shift']:.2f} 半音")
        print(
            "\n  判读：位移量越小 ⇒ 染色越少 ⇒ 越不容易听成「换人」。\n"
            "  若 B 的位移明显小于 A ⇒ 上下文续写值得落地（治本）；\n"
            "  若两者相当 ⇒ 上游不接这种用法，得另找治法（缩小死区/上限）。"
        )
    return 0 if results else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="「上下文续写」对音色分段一致性的影响（对比实验）")
    ap.add_argument("--text-file", help="自定义文案（默认内置长文）")
    args = ap.parse_args()
    try:
        return asyncio.run(_run(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 跑不起来: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
