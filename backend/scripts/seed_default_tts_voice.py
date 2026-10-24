#!/usr/bin/env python3
"""seed_default_tts_voice.py — 灌一条**默认音色**（零样本克隆音源）到音色库。

## 为什么要有这个脚本（2026-10-24）

「分段音色差异」的修法是让零样本克隆真的生效，而克隆必须有**音源**。
生产上 `tts_voices` 表当时是 **0 行** —— 音色库是空的，于是：

    resolve_voice_for_user() → source='global_config'
      → 没有 ref_audio/ref_text → 蒸馏走「上游随机音色」
      → worker 日志里 applied=False → 分块各是一个随机音色

也就是说：**代码支持克隆 ≠ 克隆在生效**。音源必须在库里。

## 为什么不手写 SQL

音色库有全套业务规则（slug 唯一、长度上限、参考音频必填、
「至多一条 is_default」的部分唯一索引、软删语义…）。
手写 SQL 绕开它们，迟早与代码里的规则对不上。
这个脚本**调服务层自己的函数**（`create_voice` / `update_voice`），
规则只有一份。

## 参考音频与参考文本必须严格对应

零样本克隆靠「参考音频 + 该音频的逐字转录」来提取音色。
转录对不上，克隆出来的音色会念错/含糊。

⚠️ 所以推荐**用已知文本现场合成**参考音（转录由构造保证一致），
而不是拿一段来路不明的录音。本仓库里的
`backend/data/voices/tingting_ref.wav` 就属于「音频在、转录无从验证」。

## 用法

    # 上传参考音频 → 建一条默认音色
    python scripts/seed_default_tts_voice.py \\
        --ref-audio /tmp/ref_candidates/cand_3.wav \\
        --ref-text "今天天气不错，我们一起来看看这条新闻讲了什么内容。" \\
        --slug tingting-clone --display-name 婷婷

    # 同 slug 已存在时：**更新**它（并保持/取消默认），不报错
    python scripts/seed_default_tts_voice.py --ref-audio ... --ref-text ... \\
        --slug tingting-clone --display-name 婷婷 --update

    # 只看当前音色库状态
    python scripts/seed_default_tts_voice.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# ── 包路径自举 ─────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

# 与音色库服务层的上限保持一致（那边是 MAX_REF_AUDIO_BYTES）
MAX_REF_AUDIO_BYTES = 10 * 1024 * 1024
MIN_REF_AUDIO_BYTES = 1000


async def _upload(data: bytes) -> str:
    """参考音频 → 存储 → 可读 URL。

    走**存储工厂**（与后台「上传参考音频」同一个入口），
    所以音色音频和成稿音频落在同一个 bucket，不需要单独配一套凭证。
    """
    from stashbox.backend.app.services.storage import get_storage

    key = f"voices/{uuid.uuid4().hex}.wav"
    url = await get_storage().save(key, data, content_type="audio/wav")
    return url


async def _list() -> int:
    from stashbox.backend.common.tts_voice_service import list_voices

    voices = await list_voices(include_inactive=True)
    if not voices:
        print("音色库是空的 —— 克隆没有音源可用（蒸馏会回落到上游随机音色）。")
        return 1
    for v in voices:
        flag = " [默认]" if v.is_default else ""
        print(f"{v.id}  slug={v.slug}  name={v.display_name}{flag}")
        print(f"    active={v.is_active}  deleted={v.deleted_at is not None}")
        print(f"    ref_audio={v.ref_audio_url}")
        print(f"    ref_text={v.ref_text}")
    return 0


async def _seed(args) -> int:
    from stashbox.backend.common.tts_voice_service import (
        VoiceError,
        create_voice,
        get_voice_by_slug,
        update_voice,
    )

    path = Path(args.ref_audio)
    if not path.is_file():
        print(f"❌ 参考音频不存在: {path}")
        return 2
    data = path.read_bytes()
    if len(data) < MIN_REF_AUDIO_BYTES:
        print(f"❌ 参考音频太小({len(data)}B)，不像有效音频")
        return 2
    if len(data) > MAX_REF_AUDIO_BYTES:
        print(f"❌ 参考音频过大({len(data) // 1024 // 1024}MB)")
        return 2
    if data[:4] != b"RIFF":
        print(f"❌ 参考音频不是 wav（RIFF 头）: {path}")
        return 2

    ref_text = (args.ref_text or "").strip()
    if not ref_text:
        print("❌ --ref-text 不能为空：参考文本必须与参考音频内容逐字对应")
        return 2

    url = await _upload(data)
    print(f"[上传] {len(data)}B → {url}")

    existing = await get_voice_by_slug(args.slug)
    try:
        if existing is not None:
            if not args.update:
                print(
                    f"❌ slug {args.slug!r} 已存在（{existing.display_name}）。"
                    f"要更新它就加 --update。"
                )
                return 1
            voice = await update_voice(
                existing.id,
                display_name=args.display_name,
                ref_audio=url,
                ref_text=ref_text,
                is_active=True,
                is_default=not args.no_default,
            )
            print(f"[更新] {voice.id} slug={voice.slug} name={voice.display_name}")
        else:
            voice = await create_voice(
                slug=args.slug,
                display_name=args.display_name,
                ref_audio=url,
                ref_text=ref_text,
                description=args.description,
                is_default=not args.no_default,
            )
            print(f"[新建] {voice.id} slug={voice.slug} name={voice.display_name}")
    except VoiceError as exc:
        print(f"❌ 音色库拒绝：{exc}")
        return 1

    print(f"[生效] is_default={voice.is_default} is_active={voice.is_active}")
    print(f"       ref_audio={voice.ref_audio_url}")
    print(f"       ref_text={voice.ref_text}")
    print(
        "\n下一步：用 backend/scripts/check_tts_voice_consistency.py 验一下"
        "（块间基频中位数差异应 < 5%）。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="灌一条默认音色（克隆音源）到音色库")
    ap.add_argument("--ref-audio", help="本地 wav 路径（会被上传到存储）")
    ap.add_argument("--ref-text", help="参考音频的逐字转录")
    ap.add_argument("--slug", default="tingting-clone", help="音色标识（默认 tingting-clone）")
    ap.add_argument("--display-name", default="婷婷", help="展示名")
    ap.add_argument("--description", default=None, help="描述")
    ap.add_argument("--no-default", action="store_true", help="不设为默认音色")
    ap.add_argument("--update", action="store_true", help="slug 已存在时更新而不是报错")
    ap.add_argument("--list", action="store_true", help="只列出现有音色")
    args = ap.parse_args()

    try:
        if args.list:
            return asyncio.run(_list())
        if not args.ref_audio:
            ap.error("需要 --ref-audio（或用 --list 看现状）")
        return asyncio.run(_seed(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
