"""长稿 TTS 分段合成 + Arq 重试契约（2026-10-21 生产事故）。

## 事故 1：长稿合成必超时

生产（tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）实测：

    1503B / ~500 字  → 40s  ✅
    1636B / ~545 字  → 41s  ✅
    3142B / ~1047 字 → 60s  ❌ 超时失败

httpx 的 timeout 是**固定 60s**，合成耗时却随字数近似线性增长 →
长稿必然击穿。表现极具迷惑性：LLM 改写正常、script_text 写进库了，
只有音频没出来，文章卡在「蒸馏失败」。

修法：按句读边界切块 → 并行合成 → 拼接（与 IndexTTS 共用实现）。

## 事故 2（同日另一处）：Arq 重试从未生效

arq 0.28 的 worker 只对 `arq.Retry` / `CancelledError` / `RetryJob`
重试；普通异常一律判 failed。distill_task 抛的是裸 RuntimeError，
于是 `ARQ_RETRY_MAX=2` 这套配置**一次都没生效过**。

修法与回归测试见 tests/ai/test_worker_distill_task.py
（那边的用例直接跑 distill_task 的失败路径）。
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


# ---------------------------------------------------------------------------
# 事故 1：TTS 分段合成
# ---------------------------------------------------------------------------


class _FakeTTSResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": "audio/mpeg"}

    def json(self):
        return {}


def _make_recorder(openai_mod, **kwargs):
    """造一个只覆写「单段合成」的客户端，记录每段输入。

    刻意继承真实类：这样走的是真正的 synthesize() 分段逻辑，
    只把「打端点」这一步换成假的（不打真网络）。
    """
    byte_size = kwargs.pop("byte_size", 1024)

    class _RecordingClient(openai_mod.OpenAITTSClient):
        def __init__(self):
            super().__init__(api_key="k", base_url="http://x", model="m", **kwargs)
            self.calls: list[str] = []

        async def _synthesize_once(self, text, voice=None, output_format="mp3"):
            self.calls.append(text)
            # 每段给一个可区分的头，方便断言拼接顺序
            return b"SEG" + str(len(self.calls)).encode() + b"\x00" * byte_size

    return _RecordingClient()


@pytest.fixture
def openai_mod():
    from app.services.tts import openai as mod

    return mod


@pytest.mark.asyncio
async def test_long_text_is_split_into_chunks(openai_mod):
    """长稿必须切成多段，分别调用端点。"""
    recorder = _make_recorder(openai_mod, chunk_chars=100, chunk_concurrency=2)
    text = "这是一句话。" * 60  # 360 字 → 至少 4 块

    out = await recorder.synthesize(text)

    assert len(recorder.calls) >= 4, f"应该切块，实际 {len(recorder.calls)} 次调用"
    assert all(len(c) <= 100 for c in recorder.calls), "每块都不该超过 chunk_chars"
    assert len(out) > 0


@pytest.mark.asyncio
async def test_short_text_is_not_split(openai_mod):
    """短稿不切块 —— 保持单次请求（管理后台的连通性测试走这条路）。"""
    recorder = _make_recorder(openai_mod, chunk_chars=300)

    await recorder.synthesize("短文本。")

    assert recorder.calls == ["短文本。"]


@pytest.mark.asyncio
async def test_chunks_keep_original_order(openai_mod):
    """拼接顺序必须与原文顺序一致（gather 保序）。"""
    recorder = _make_recorder(openai_mod, chunk_chars=50, chunk_concurrency=4)
    text = "第一段。" * 10 + "第二段。" * 10

    out = await recorder.synthesize(text)

    assert len(recorder.calls) >= 2
    # 头部里记着调用序号，拼接后应严格递增
    assert out.startswith(b"SEG1")


@pytest.mark.asyncio
async def test_empty_text_still_rejected(openai_mod):
    """空文本仍然报错（分段不能绕过这个校验）。"""
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    with pytest.raises(ValueError):
        await client.synthesize("   ")


def test_mp3_concat_is_plain_byte_concat():
    """mp3 每帧自包含 → 直接字节拼接，不该走 ffmpeg。"""
    from app.services.tts.audio_chunks import concat_audio

    a = b"\xff\xfb\x90\x00" + b"aaaa"
    b = b"\xff\xfb\x90\x00" + b"bbbb"
    assert concat_audio([a, b]) == a + b


def test_default_concurrency_is_serial(openai_mod):
    """默认并发必须是 1（串行）。

    实测（2026-10-21，3 段 × ~280 字）：
        并发=1 → 总耗时 54.2s，每段 17.4s   ← 安全
        并发=2 → 总耗时 51.1s
        并发=4 → 总耗时 53.4s，末段 53.4s  ← 逼近 60s 超时线
    三种并发总耗时一样 → 上游串行处理，并发零吞吐收益，
    反而让末段在队列里等到超时（生产实跑第 4 段确实炸了）。

    这个断言就是为了挡住「有人看到『并发』就顺手调到 4」。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    assert client.chunk_concurrency == 1, "默认必须是串行（上游实测串行，并发只会让末段超时）"
    assert client.chunk_chars == 300


def test_env_can_override_chunk_settings(openai_mod, monkeypatch):
    """chunk 大小与并发仍可由 env 覆盖（运营调参不用改代码）。"""
    monkeypatch.setenv("OPENAI_TTS_CHUNK_CHARS", "200")
    monkeypatch.setenv("OPENAI_TTS_CONCURRENCY", "2")
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    assert client.chunk_chars == 200
    assert client.chunk_concurrency == 2


def test_wav_concat_uses_ffmpeg_or_degrades():
    """wav 走 ffmpeg/退化路径，长度必须正确（不能是首段的长度）。"""
    from app.services.tts.audio_chunks import concat_wav

    def _wav(payload: bytes) -> bytes:
        header = b"RIFF" + (36 + len(payload)).to_bytes(4, "little") + b"WAVEfmt "
        header += b"\x10\x00\x00\x00" + b"\x01\x00\x01\x00"
        header += (8000).to_bytes(4, "little") + (16000).to_bytes(4, "little")
        header += b"\x02\x00\x10\x00" + b"data" + len(payload).to_bytes(4, "little")
        return header + payload

    a = _wav(b"A" * 100)
    b = _wav(b"B" * 100)
    merged = concat_wav([a, b])
    assert len(merged) > len(a), "拼接后不能只有首段"
    assert b"B" * 100 in merged


# ---------------------------------------------------------------------------
# 事故 2：Arq 重试契约
# ---------------------------------------------------------------------------
