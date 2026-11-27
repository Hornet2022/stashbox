"""CP-TTS-PRESET（迁移 0042）：预设路径的**请求体**契约（openai 层，mock）。

tools 层的守卫（tests/ai/test_tts_voice_preset.py）钉的是「tools 怎么调 synthesize」；
这里钉的是「synthesize 不带 ref 时，**真正发给上游的请求体**长什么样」——
两层合起来才覆盖整条链（守卫测了 helper ≠ 守卫测了调用点，反之亦然）。

预设 = 固定说话人，机制是：不传 ref_audio ⇒ `_resolve_clone` 返回 None ⇒
请求体**只有 voice、没有 ref_***⇒ 上游按 voice 名渲染（跨请求身份稳定）。
克隆模式上游忽略 voice；预设模式 voice 才生效。所以这里钉死：

1. 预设请求体带 voice=预设名，且**无 ref_audio/ref_text**。
2. **多块**预设渲染：每一块都带同一个 voice、都没有 ref（少一块带 ref 就是那块换人）。
3. 预设走**普通块长预算**（`OPENAI_TTS_CHUNK_CHARS`），不被克隆块长夹 ——
   生产把普通块长提到 1500 就是给预设用的（墙钟实证 ≪ 300s 超时）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts import openai as openai_module  # noqa: E402
from app.services.tts.openai import OpenAITTSClient  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "audio/mpeg"}
        self.text = ""


@pytest.fixture(autouse=True)
def _reset_circuit():
    openai_module._circuit_state["consecutive_failures"] = 0
    openai_module._circuit_state["opened_at"] = 0.0
    yield
    openai_module._circuit_state["consecutive_failures"] = 0
    openai_module._circuit_state["opened_at"] = 0.0


@pytest.fixture
def concat_stub(monkeypatch):
    monkeypatch.setattr(openai_module, "concat_audio", lambda pieces, gap_sec=0.3: b"".join(pieces))


def _client(chunk_chars: int = 1000, **kwargs) -> OpenAITTSClient:
    return OpenAITTSClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="Qwen3-TTS-12Hz-1.7B-CustomVoice",
        voice="uncle_fu",
        chunk_chars=chunk_chars,
        chunk_retries=0,
        **kwargs,
    )


def _install_post(client, handler):
    bodies: list[dict] = []

    async def _post(url, json=None, **kw):
        bodies.append(json)
        return handler(json or {})

    client._client.post = _post  # type: ignore[method-assign]
    return bodies


async def test_预设请求体带voice且无ref(concat_stub):
    """不带 ref ⇒ clone=None ⇒ 请求体只有 voice，没有 ref_*（voice 这才生效）。"""
    client = _client(chunk_chars=1000)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize("一段短文本。", voice="uncle_fu")

    assert len(bodies) == 1
    assert bodies[0]["voice"] == "uncle_fu"
    assert "ref_audio" not in bodies[0], "预设路径带了 ref_audio ⇒ 退回克隆采样"
    assert "ref_text" not in bodies[0]


async def test_预设多块每块带voice且无ref(concat_stub):
    """多块预设渲染：每块同一个 voice、都没有 ref。

    预设的稳定性靠「每块都是同一个固定说话人」—— 任何一块带了 ref 就那块换人。
    """
    client = _client(chunk_chars=8)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize("第一句话。第二句话。第三句话。第四句话。第五句话。", voice="uncle_fu")

    assert len(bodies) >= 3, "这段文本应该切出多块"
    assert all(b["voice"] == "uncle_fu" for b in bodies), "有块没带预设 voice"
    assert all(
        "ref_audio" not in b and "ref_text" not in b for b in bodies
    ), "有块带了 ref ⇒ 那块退回克隆采样 ⇒ 接缝换人"


async def test_预设走普通块长预算不被克隆夹(concat_stub):
    """预设（无 ref）用 chunk_chars，不用 clone_chunk_chars。

    生产把 OPENAI_TTS_CHUNK_CHARS 提到 1500 给预设用；克隆块长（另一套）不该夹它。
    900 字在普通块长 1500 下应是 1 块（即便克隆块长配成 800）。
    """
    client = _client(chunk_chars=1500, clone_chunk_chars=800)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))
    text = "这是一段用来测试分块的中文文本。" * 60  # 900 字

    await client.synthesize(text, voice="uncle_fu")

    assert len(bodies) == 1, (
        f"预设路径被克隆块长夹住了，切出 {len(bodies)} 块 —— "
        f"预设走普通块长（1500），900 字应是 1 块"
    )
