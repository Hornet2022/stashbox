#!/usr/bin/env python3
"""exp_tts_identity_audit.py —— 音色**身份**层审计的素材采集器。

## 为什么要有这个脚本（2026-11-25）

内测反馈「分段音色不一致，明显换人感」。前一轮用「段间基频散布」当判据做 A/B，
**判据本身是坏的**，两条实测把它否掉：

1. 仓内既有结论（`check_tts_voice_consistency.py`）：
   **跨块比基频不成立** —— 不同文本的固有音高差异实测可达 13%，
   比 5% 的判据还大 ⇒ 跨块量到的是「文案差异」，不是「换没换人」。
2. 本轮实测的噪声底（`exp_tts_metric_noise_floor.py`，当前上游）：
   **同一段文本**重渲 4 次，median 散布 **14.0%**、p25 散布 **12.3%**
   ⇒ 连「同文重渲」都飘 14%，「跨块散布」这个量根本区分不出任何东西。

⇒ 必须换一把尺子：**ECAPA-TDNN 说话人嵌入余弦**（身份层，与音高/响度无关）。
仓内已标定过分离带（2026-10-25，但**换过上游，必须重标**）：

    同一人（克隆段 / 同音色另一次渲染）  整段余弦 0.75 ~ 0.80
    同一段文本重渲一次                   整段余弦 0.93 ~ 0.95
    换人（不同音色）                      整段余弦 0.29 ~ 0.35
    ⇒ 门禁阈值 0.55（`consistency.ECAPA_THRESHOLD`）

本脚本**只采集素材**（把音频落到磁盘 + 记 manifest），
判定在离线机器上做（生产 venv 没有 torch/speechbrain）。
⇒ 素材 + 本地 ECAPA 才是完整的实验。

## 采什么

    ref/default.mp3        默认音色的**克隆源参考音**（身份基准）
    ref/other.mp3          另一个音色的参考音（对照组：另一个「人」）
    A/chunk_NN.raw.mp3     A 现状（逐块同一份参考音）—— 合成出来的原始块
    A/chunk_NN.aligned.mp3 同一块经过 `align_pitch` 之后（量「移调染色」）
    B/chunk_NN.raw.mp3     B 上下文续写（块 N 的输出当块 N+1 的参考）
    B/chunk_NN.aligned.mp3
    NOISE/chunk_01.rN.mp3  **同一段文本**重渲 N 次（身份层的噪声底）
    OTHER/chunk_01.mp3     用另一个音色克隆同一段文本（换人带）
    manifest.json          每块的文本长度 / p25 / median / 对齐位移半音

⇒ 本地 ECAPA 跑完后能回答四个问题：

    Q1 上游逐块采样时，是不是同一个人？   → cos(ref, A/chunk_NN.raw)
    Q2 后处理移调有没有把「同一个人」染色？ → cos(ref, raw) vs cos(ref, aligned)
    Q3 上下文续写有没有改善身份一致性？     → A vs B 的 raw 余弦
    Q4 当前上游的分离带是多少？             → NOISE（同人带）与 OTHER（换人带）

## 用法（生产）

    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_tts_identity_audit.py --out-dir /tmp/tts_identity

    # 自带文案（必须是 ≥1600 字，否则只切得出 1~2 块，测不出段间差异）
    python scripts/exp_tts_identity_audit.py --text-file /path/to/script.txt --out-dir /tmp/tts_identity

退出码：0 = 素材采齐（看 stdout 的清单）；2 = 跑不起来。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
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


async def _voice_rows() -> list:
    """音色库里的 active 音色（默认音色排第一）。"""
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=False)
    default = next((v for v in voices if v.is_default), None)
    if default is None:
        raise SystemExit("音色库没有默认音色 —— 克隆没有音源可用。")
    others = [v for v in voices if v.id != default.id]
    return [default, *others]


async def _dump_ref_audio(voice, out_path: Path) -> int:
    """把音色的参考音下载到磁盘（身份基准）。返回字节数。"""
    from stashbox.backend.app.services.tts.ref_audio import load_ref_audio_data_url

    data_url = await load_ref_audio_data_url(voice.ref_audio_url)
    head, _, b64 = data_url.partition(",")
    import base64

    data = base64.b64decode(b64)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return len(data)


def _clip_for_ref(audio: bytes, text: str, max_sec: float) -> tuple[bytes, str]:
    """把上一块的输出裁成「前 max_sec 秒 + 对应的前段文本」。

    ⚠️ 上游参考音有 30 秒硬上限（实测报
       `Reference audio too long (114.7s). Maximum 30s supported`）。
    ⚠️ 参考文本必须与参考音频**内容对应**（上游要求两者一致），
       所以按时长比例截断文本。
    """
    from stashbox.backend.app.services.tts.voice_align import _decode_pcm

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
        fh.write(audio)
        src = fh.name
    out = src + ".clip.mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src, "-t", str(max_sec), out],
            check=True,
        )
        clipped = Path(out).read_bytes()
    finally:
        for p in (src, out):
            try:
                Path(p).unlink()
            except OSError:
                pass

    samples = _decode_pcm(audio)
    total = len(samples) / 16000.0 if samples else max_sec
    ratio = min(1.0, max_sec / total) if total > 0 else 1.0
    n_chars = max(20, int(len(text) * ratio))
    return clipped, text[:n_chars]


async def _render_group(
    client,
    chunks: list[str],
    base_spec,
    out_dir: Path,
    group: str,
    *,
    contextual: bool,
    ref_max_sec: float = 30.0,
) -> list[dict]:
    """渲染一组，保存 raw + aligned 两套音频，返回 manifest 行。"""
    from stashbox.backend.app.services.tts.openai import _CloneSpec
    from stashbox.backend.app.services.tts.ref_audio import load_ref_audio_data_url
    from stashbox.backend.app.services.tts.voice_align import align_pitch, f0_profile

    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    raw_pieces: list[bytes] = []
    tmpdir = Path(tempfile.mkdtemp(prefix=f"tts_id_{group}_"))

    prev_audio: bytes | None = None
    prev_text: str | None = None

    for i, chunk in enumerate(chunks, 1):
        if contextual and prev_audio is not None and prev_text:
            clip, prev_text = _clip_for_ref(prev_audio, prev_text, ref_max_sec)
            fpath = tmpdir / f"ref_{i}.mp3"
            fpath.write_bytes(clip)
            spec = _CloneSpec(
                source=str(fpath),
                data_url=await load_ref_audio_data_url(str(fpath)),
                text=prev_text,
            )
        else:
            spec = base_spec

        audio = await client._synthesize_chunk(chunk, voice=client.voice, clone=spec)
        raw_pieces.append(audio)
        prev_audio, prev_text = audio, chunk

        (out_dir / f"chunk_{i:02d}.raw.mp3").write_bytes(audio)
        prof = f0_profile(audio)
        rows.append(
            {
                "group": group,
                "index": i,
                "chars": len(chunk),
                "p25": prof.p25,
                "median": prof.median,
                "frame_count": prof.frame_count,
                "duration_sec": prof.duration_sec,
                "raw": f"chunk_{i:02d}.raw.mp3",
            }
        )
        print(
            f"  [{group}] 块 {i}: {len(chunk)} 字  p25={prof.p25} median={prof.median} "
            f"帧数={prof.frame_count}",
            flush=True,
        )

    # ── 对齐后的一套（量「移调染色」）──────────────────────────────
    aligned, details = align_pitch(raw_pieces)
    for row, piece, det in zip(rows, aligned, details):
        (out_dir / row["raw"].replace(".raw.", ".aligned.")).write_bytes(piece)
        row["aligned"] = row["raw"].replace(".raw.", ".aligned.")
        row["semitones"] = det.get("semitones")
        row["f0_before"] = det.get("f0_before")
        row["f0_after"] = det.get("f0_after")
        row["skipped"] = det.get("skipped")
    shifts = [r["semitones"] for r in rows if r.get("semitones") is not None]
    print(
        f"  [{group}] 对齐位移：{' / '.join(f'{s:+.2f}' for s in shifts)} 半音"
        f"（最大 {max((abs(s) for s in shifts), default=0.0):.2f}）",
        flush=True,
    )
    return rows


async def _render_single(client, text: str, spec, out_path: Path, tag: str) -> dict:
    """渲染一段（对照组用），保存 raw 音频。"""
    from stashbox.backend.app.services.tts.voice_align import f0_profile

    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio = await client._synthesize_chunk(text, voice=client.voice, clone=spec)
    out_path.write_bytes(audio)
    prof = f0_profile(audio)
    print(
        f"  [{tag}] {len(text)} 字  p25={prof.p25} median={prof.median}",
        flush=True,
    )
    return {
        "group": tag,
        "chars": len(text),
        "p25": prof.p25,
        "median": prof.median,
        "frame_count": prof.frame_count,
        "duration_sec": prof.duration_sec,
        "raw": out_path.name,
    }


async def _run(args) -> int:
    from stashbox.backend.app.services.tts import reload as tts_reload
    from stashbox.backend.app.services.tts.audio_chunks import split_into_chunks

    text = (
        Path(args.text_file).read_text(encoding="utf-8").strip() if args.text_file else DEFAULT_TEXT
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    voices = await _voice_rows()
    default = voices[0]
    print(f"[音色] 默认音色：{default.display_name}（slug={default.slug}）", flush=True)
    print(f"[音色] 音色库共 {len(voices)} 个 active 音色", flush=True)

    client = await tts_reload()
    chunk_chars = int(getattr(client, "clone_chunk_chars", 800) or 800)
    print(f"[客户端] provider={client.provider_name} 克隆块长={chunk_chars} 字", flush=True)

    chunks = split_into_chunks(text, chunk_chars)
    print(f"[分块] {len(text)} 字 → {len(chunks)} 块", flush=True)
    if len(chunks) < 2:
        print("⚠️ 只切出 1 块 —— 测不出段间差异，换更长的文案（≥1600 字）", flush=True)

    manifest: dict = {"chunks": len(chunks), "chunk_chars": chunk_chars, "rows": []}

    # ── 参考音（身份基准）────────────────────────────────────────
    print("\n▶ 采集参考音", flush=True)
    n = await _dump_ref_audio(default, out_dir / "ref" / "default.mp3")
    manifest["ref_default"] = {
        "display_name": default.display_name,
        "slug": default.slug,
        "bytes": n,
    }
    print(f"  ref/default.mp3  {n} bytes", flush=True)

    other = voices[1] if len(voices) > 1 else None
    if other is not None:
        n = await _dump_ref_audio(other, out_dir / "ref" / "other.mp3")
        manifest["ref_other"] = {"display_name": other.display_name, "slug": other.slug, "bytes": n}
        print(f"  ref/other.mp3    {n} bytes（对照组：{other.display_name}）", flush=True)

    base_spec = await client._resolve_clone(default.ref_audio_url, default.ref_text)
    if base_spec is None:
        raise SystemExit("拿不到克隆参数（ref_audio/ref_text 不齐）")

    # ── A 现状 ────────────────────────────────────────────────────
    print("\n▶ A 现状（逐块同一份参考音）", flush=True)
    manifest["rows"] += await _render_group(
        client, chunks, base_spec, out_dir / "A", "A", contextual=False
    )

    # ── B 上下文续写 ─────────────────────────────────────────────
    print("\n▶ B 上下文续写（块 N 的输出当块 N+1 的参考）", flush=True)
    manifest["rows"] += await _render_group(
        client, chunks, base_spec, out_dir / "B", "B", contextual=True
    )

    # ── NOISE：同一段文本重渲 N 次（身份层噪声底）────────────────
    print(f"\n▶ NOISE 同一段文本重渲 {args.repeat} 次（块 1 的文本）", flush=True)
    probe = chunks[0]
    for r in range(1, args.repeat + 1):
        row = await _render_single(
            client, probe, base_spec, out_dir / "NOISE" / f"chunk_01.r{r}.mp3", "NOISE"
        )
        row["index"] = r
        manifest["rows"].append(row)

    # ── OTHER：另一个音色克隆同一段文本（换人带）──────────────────
    if other is not None:
        print(f"\n▶ OTHER 用另一个音色（{other.display_name}）克隆同一段文本", flush=True)
        other_spec = await client._resolve_clone(other.ref_audio_url, other.ref_text)
        if other_spec is not None:
            row = await _render_single(
                client, probe, other_spec, out_dir / "OTHER" / "chunk_01.mp3", "OTHER"
            )
            manifest["rows"].append(row)
        else:
            print("  ✗ 另一个音色的克隆参数拿不到，跳过", flush=True)

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n✅ 素材采齐 → {out_dir}", flush=True)
    print(f"   manifest: {out_dir / 'manifest.json'}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="音色身份（ECAPA）审计素材采集")
    ap.add_argument("--text-file", help="自定义文案（默认内置长文）")
    ap.add_argument("--out-dir", default="/tmp/tts_identity", help="素材输出目录")
    ap.add_argument("--repeat", type=int, default=3, help="噪声底重渲几次（默认 3）")
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
