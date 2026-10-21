"""音频 key 必须随内容变化（缓存失效契约）。

## 事故

2026-10-21 内测反馈：uid=15 的账户「蒸馏的内容是对的，但是语音不对了」。

截图里的硬证据：播放条停在 `1:01 / 1:01`，而服务端音频实际是 99.3 秒。

排查结论（真机实测对照）：

- 客户端显示 `3:43 / 3:43`（223 秒），而服务端**当前**文件是 184.032 秒
  —— 客户端播的不是服务端现在这个文件。
- 223.352 秒 = 19:10 那一版的时长（重跑前的旧文件）。客户端播的是**旧版本**。
- 原因：客户端走 Media3 `SimpleCache`，**按 URL 做缓存 key，且对已缓存的 URL
  永不向服务器重新验证**。而音频 URL 一直是 `audio/{article_id}.{ext}` ——
  重跑换了内容却没换 URL → 缓存永远命中旧的。
- Range 请求正常（206 + 正确 content-range），文件结构正常（Xing 头只有 1 处、
  逐帧参数一致），排除传输与文件损坏。

于是稿子（script_text，从 API 拿）是新的，语音（音频，从缓存拿）是旧的。

## 修法

音频 key 带**内容 hash**（`audio/{article_id}.{hash8}.{ext}`）：
内容变 → key 变 → URL 变 → 缓存自然失效。local 与 oss 都成立
（oss 的 presigned URL 由 key 决定，且 presigned URL 不能额外拼 query）。
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.storage.base import (  # noqa: E402
    content_hash,
    versioned_audio_key,
    versioned_variant_key,
)


def test_key_changes_when_content_changes():
    """内容变 → key 必须变（这是整个修复的核心）。"""
    a = versioned_audio_key("art_1", "mp3", b"audio-bytes-A")
    b = versioned_audio_key("art_1", "mp3", b"audio-bytes-B")
    assert a != b, "不同内容必须产生不同 key，否则客户端缓存不失效"


def test_key_is_stable_for_same_content():
    """相同内容 → 相同 key（幂等：重跑同样结果不会产生新文件）。"""
    a = versioned_audio_key("art_1", "mp3", b"same-bytes")
    b = versioned_audio_key("art_1", "mp3", b"same-bytes")
    assert a == b


def test_key_shape_keeps_article_id_and_ext():
    """key 形态：audio/{article_id}.{hash8}.{ext} —— 后缀仍是合法的音频扩展名。

    扩展名不能丢：客户端 ExoPlayer 按后缀选 decoder，
    content-service 的 _resolve_actual_audio_extension 也按后缀找文件。
    """
    key = versioned_audio_key("art_260758b8889240bf87326117", "mp3", b"x")
    assert key.startswith("audio/art_260758b8889240bf87326117.")
    assert key.endswith(".mp3")
    # 中间是 8 位 hash
    middle = key[len("audio/art_260758b8889240bf87326117.") : -len(".mp3")]
    assert len(middle) == 8
    assert all(c in "0123456789abcdef" for c in middle)


def test_variant_key_also_versioned():
    """变体（多码率 warm）同样会重新生成，key 也要带 hash。"""
    a = versioned_variant_key("art_1", 64, "m4a", b"v1")
    b = versioned_variant_key("art_1", 64, "m4a", b"v2")
    assert a != b
    assert a.startswith("audio/art_1.64k.")
    assert a.endswith(".m4a")


def test_content_hash_is_sha256_prefix():
    """hash 用 sha256 前 8 位（16 进制）—— 够区分内容，又不会让文件名过长。"""
    import hashlib

    data = b"hello"
    assert content_hash(data) == hashlib.sha256(data).hexdigest()[:8]
    assert len(content_hash(data)) == 8


def test_resolve_actual_audio_extension_still_finds_versioned_file(tmp_path):
    """content-service 的扩展名兜底扫描要能认出带 hash 的文件名。

    它按 `Path(url).stem` 找磁盘文件 —— 文件名多一段 hash 后，
    stem 变成 `art_x.deadbeef`，`with_suffix('.mp3')` 仍应指向真实文件。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_keyver_content_main", BACKEND_DIR / "content-service" / "main.py"
    )
    main = importlib.util.module_from_spec(spec)
    sys.modules["_keyver_content_main"] = main
    spec.loader.exec_module(main)

    (tmp_path / "art_1.deadbeef.mp3").write_bytes(b"x" * 10)

    resolved = main._resolve_actual_audio_extension(
        "https://gw.example/audio/audio/art_1.deadbeef.mp3", str(tmp_path)
    )
    assert resolved.endswith("/audio/art_1.deadbeef.mp3"), resolved


@pytest.mark.parametrize("data", [b"", b"\x00", b"x" * 1000])
def test_key_generation_never_raises(data):
    """空内容也要能生成 key（不抛错 —— 抛错会让整个蒸馏失败）。"""
    key = versioned_audio_key("art_1", "mp3", data)
    assert key.startswith("audio/art_1.")
