"""本地音频存储的**重复落盘**回归（2026-10-22 实测）。

## 现象

生产上 `INDEXTTS_AUDIO_DIR` 与 `LOCAL_AUDIO_DIR` **是同一个目录**
（`/var/lib/stashbox/data/audio`）：

- `agent/tools.py` 的 TTS 工具把合成结果落盘成 `{article_id}.{fmt}`（无 hash）
- `app/services/storage` 的产物落盘成 `{article_id}.{hash8}.{fmt}`（内容寻址）

→ **同一份音频存了两遍**，而且带 hash 的那份是 DB 真正引用的，
不带 hash 的那份**没人引用、也没人删**。

实测（2026-10-22，服务器）：音频目录 35 个文件里 **21 个是孤儿（46.9MB，占 58%）**，
其中 15 个与产物**逐字节相同** —— 磁盘整整多占一倍，且每次蒸馏都发生、无回收。

## 钉子

1. TTS 工具的本地落盘必须与 storage 产物**同一个文件名**
   → 同一目录里合成一次只留一个文件。
2. 兜底的音频 URL 解析（`_resolve_actual_audio_extension`）必须认得
   **内容寻址**的文件名（`{article_id}.{hash8}.{ext}`），
   并且取**最新**的那个、且不认多码率变体。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _FakeStorage:
    """记录 save() 的 key，并真的落盘（用来验证「同一目录只留一个文件」）。"""

    def __init__(self, root: Path):
        self.root = root
        self.saved: list[str] = []

    async def save(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        self.saved.append(key)
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"https://cdn.example.com/{key}"


class _FakeTTSClient:
    provider_name = "indextts"
    voice = None

    def __init__(self, audio: bytes):
        self._audio = audio

    async def synthesize(self, text, voice=None, **kwargs) -> bytes:
        return self._audio


class _Resolved:
    ref_audio = ""
    ref_text = ""
    voice_id = None
    display_name = None
    source = "global_config"


@pytest.mark.asyncio
async def test_tts_local_write_shares_filename_with_storage_artifact(tmp_path, monkeypatch):
    """TTS 工具的本地落盘与 storage 产物必须是**同一个文件**。

    生产上两个目录是同一个（INDEXTTS_AUDIO_DIR == LOCAL_AUDIO_DIR），
    命名不一致就等于把同一份音频存两遍 —— 磁盘翻倍，且多出来那份永远没人删。
    """
    from agent import tools as tools_module

    audio = b"ID3\x04\x00\x00\x00" + b"fake-mp3-payload" * 500
    # 生产布局：LOCAL_AUDIO_DIR=/var/lib/stashbox/data，
    # 而 INDEXTTS_AUDIO_DIR=/var/lib/stashbox/data/audio
    # —— 正好是 storage 的 key 前缀 `audio/` 落出来的那个子目录。
    # 也就是说**两次写落在同一个目录**，命名不一致就必然存两遍。
    storage_root = tmp_path / "data"
    shared_dir = storage_root / "audio"
    shared_dir.mkdir(parents=True)

    monkeypatch.setenv("INDEXTTS_AUDIO_DIR", str(shared_dir))
    monkeypatch.setattr(
        "stashbox.backend.common.tts_voice_service.resolve_voice_for_user",
        lambda *a, **k: _make_coro(_Resolved()),
    )
    monkeypatch.setattr(
        "stashbox.backend.app.services.tts.reload",
        lambda *a, **k: _make_coro(_FakeTTSClient(audio)),
    )
    storage = _FakeStorage(storage_root)
    monkeypatch.setattr("stashbox.backend.app.services.storage.get_storage", lambda: storage)

    out = await tools_module.tts_synthesize_tool(
        {"article_id": "art_dup_check", "rewritten_script": "一段稿子"}, {}
    )

    # storage 确实写了产物
    assert storage.saved, "storage 没被调用"
    key = storage.saved[0]

    # 本地落盘的路径 == storage 产物的同一个文件
    local_path = Path(out["audio_path"])
    storage_path = shared_dir / key
    assert (
        local_path.name == storage_path.name
    ), f"本地落盘 {local_path.name} 与 storage 产物 {storage_path.name} 不同名 → 同一份音频存两遍"

    # 同一目录里，合成一次之后只剩**一个**音频文件
    files = [f for f in shared_dir.rglob("*") if f.is_file()]
    assert len(files) == 1, f"期望只剩 1 个文件，实得 {len(files)}：{[f.name for f in files]}"


def _make_coro(value):
    async def _c():
        return value

    return _c()
