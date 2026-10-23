"""分段合成的**音色一致性**（2026-10-23）。

## 要解决的问题

模型切到 `Qwen3-TTS-12Hz-1.7B-CustomVoice` 之后，音色/风格不再由
参考音频决定，而是由 **voice（预设 speaker 名）+ instructions（风格控制
prompt）** 决定。长稿是**分段合成**的 —— 每一段都是**独立的一次请求**，
上游每次都重新采样。三者只要有一个不固定，段与段之间就会漂。

## 这里锁住的保证

1. 同一篇的**每一段**都带**完全相同**的 voice / instructions；
2. **不**向上游发它不认的采样参数（temperature / top_p）——
   生产端点的请求体 schema 里没有这两个字段，传了被**静默丢弃**，
   于是「以为固定了采样、实际每次都重随机」（比不配更糟：它让人
   以为漂移的原因已排除）；
3. CustomVoice 模式**不传** ref_audio（否则上游的克隆分支先命中，
   把 instructions 悄悄丢掉）；
4. 自建端点（indextts provider）**能**收采样参数 → 那里必须显式固定。

## ⚠️ 这个测试为什么在 HTTP 层拦截

第一版把 payload 组装逻辑**抄了一份**进测试里 —— 那样测的是**抄写品**，
真实实现改了测试照样绿（本项目已经吃过一次同型亏：夹具让缓存命中，
被测代码根本没执行）。

所以这里用 `httpx.MockTransport` 拦在**传输层**：真实的
`_synthesize_once()` 会被真的调用，payload 由真实实现组装，
测试只把发出去的东西抓下来断言。
"""

from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts.indextts import IndexTTSClient  # noqa: E402
from app.services.tts.openai import OpenAITTSClient  # noqa: E402


def _wav(seconds: float = 0.2, rate: int = 24000) -> bytes:
    """一段最小可用的 wav（拼接/时长解析都要能吃下它）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _Recorder:
    """在传输层抓 payload —— 真实实现照跑，只是不真的发出去。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content.decode("utf-8")))
        # 一律回 wav：openai 侧走 concat_wav、indextts 侧走 _concat_wav，
        # 纯 Python 拼接不依赖 ffmpeg，测试机上不必装。
        #
        # ⚠️ 时长要**够长**：两个 provider 都有「按语速判静默截断」的校验
        # （>5.5 / >7.0 字/秒就报错）。mock 若只给 0.2s 音频，几十字的
        # 测试文本会算出几百字/秒 → 被判成「截断」→ 测试挂在**校验**上
        # 而不是挂在被测的 payload 逻辑上。15s 足够让任何用例通过校验。
        return httpx.Response(
            200,
            content=_wav(seconds=15.0),
            headers={"content-type": "audio/wav"},
        )


def _openai_client(rec: _Recorder, **kwargs) -> OpenAITTSClient:
    params = {
        "api_key": "k",
        "base_url": "http://tts.invalid/v1",
        "model": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "uncle_fu",
    }
    params.update(kwargs)
    client = OpenAITTSClient(**params)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(rec.handler),
        headers={"Authorization": "Bearer k", "Content-Type": "application/json"},
    )
    return client


def _indextts_client(rec: _Recorder, **kwargs) -> IndexTTSClient:
    params = {
        "base_url": "http://tts.invalid/v1",
        "model": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    }
    params.update(kwargs)
    client = IndexTTSClient(**params)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    return client


# ── openai provider（生产链路）───────────────────────────────────────────────


async def test_每一段都带完全相同的_voice_与_instructions():
    """分段合成时，每段的 voice/instructions 必须**逐字相同**。

    这是「分段音色一致」的**请求侧**保证：它们是实例属性，
    每段读同一份 → 段间风格取向不漂。
    """
    rec = _Recorder()
    client = _openai_client(
        rec,
        instructions="用平稳、客观的播音腔朗读，语速适中。",
    )
    client.chunk_chars = 60  # 强制切段
    text = "今天我们聊一聊怎么把一段长稿念得稳稳的。" * 12

    await client.synthesize(text, output_format="wav")

    assert len(rec.payloads) >= 2, "这段文本应该被切成多段"
    first = rec.payloads[0]
    for p in rec.payloads[1:]:
        assert p.get("voice") == first.get("voice"), "分段之间 voice 不一致"
        assert p.get("instructions") == first.get("instructions"), "分段之间 instructions 不一致"
        assert p.get("instructions") == "用平稳、客观的播音腔朗读，语速适中。"


async def test_风格控制_prompt_真的发给了上游():
    """instructions 必须出现在请求体里 —— 否则「风格控制」是自欺欺人。"""
    rec = _Recorder()
    client = _openai_client(rec, instructions="用温柔舒缓的语气说话。")

    await client.synthesize("短文本。", output_format="wav")

    assert rec.payloads
    assert rec.payloads[0].get("instructions") == "用温柔舒缓的语气说话。"


async def test_不配_instructions_时就不发这个字段():
    """没配就别发空串 —— 上游对空串的行为未定义，别赌。"""
    rec = _Recorder()
    client = _openai_client(rec)

    await client.synthesize("短文本。", output_format="wav")

    assert rec.payloads
    assert "instructions" not in rec.payloads[0]
    assert "language" not in rec.payloads[0]


async def test_不向上游发它不认的采样参数():
    """temperature / top_p **不在**生产端点的请求体 schema 里。

    实测：传 `"abc"` 也返回 200（被静默丢弃），传数字同样没有 400 ——
    上游压根没有这两个字段。所以我们**不发**：发了会让人误以为
    「采样参数已固定、漂移的原因已排除」，而实际每次请求都重随机。

    真正压住段间漂移的是后处理（音域对齐 + 响度归一 + crossfade）。
    """
    rec = _Recorder()
    client = _openai_client(rec, instructions="播音腔。")

    await client.synthesize("短文本。", output_format="wav")

    assert rec.payloads
    payload = rec.payloads[0]
    assert "temperature" not in payload, "上游不认 temperature，发了等于骗自己"
    assert "top_p" not in payload, "上游不认 top_p，发了等于骗自己"
    assert "seed" not in payload, "seed 虽然被 schema 接受，但实测不影响生成"


async def test_language_只在配了的时候发():
    rec = _Recorder()
    client = _openai_client(rec, language="Chinese")

    await client.synthesize("短文本。", output_format="wav")

    assert rec.payloads[0].get("language") == "Chinese"


# ── indextts provider（自建端点）────────────────────────────────────────────


async def test_indextts_custom_voice_模式不传_ref_audio():
    """配了 speaker/instruct 就不再传 ref_audio。

    上游的 has_ref 分支会**先**命中克隆路径，把 instructions 静默丢掉 ——
    于是「以为在用风格控制、实际音色在漂」，且日志里看不出任何异常。
    """
    rec = _Recorder()
    ref = Path("/tmp/stashbox-ref-does-not-exist.wav")
    client = _indextts_client(
        rec,
        speaker="Vivian",
        instruct="温和的中文女声",
        ref_audio_path=str(ref),
        ref_text="参考文本",
    )
    assert client.use_custom_voice is True

    await client.synthesize("短文本，不触发分段。")

    assert rec.payloads
    for p in rec.payloads:
        assert "ref_audio" not in p, "CustomVoice 模式不应带 ref_audio"
        assert "ref_text" not in p, "CustomVoice 模式不应带 ref_text"
        assert p.get("voice") == "Vivian"
        assert p.get("instruct") == "温和的中文女声"


async def test_indextts_克隆模式不传_voice_和_instruct(tmp_path):
    """克隆路径要求 voice/instruct 都是 None（上游 can_generate 会判 False）。"""
    rec = _Recorder()
    # 参考音频必须是**真实存在**的文件：克隆模式会先读它（读不到就直接
    # 抛错，请求根本发不出去 → 也就没东西可断言）。
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav(seconds=0.2) + b"\x00" * 1200)  # >1000B 的下限
    client = _indextts_client(rec, ref_audio_path=str(ref), ref_text="参考文本")
    assert client.use_custom_voice is False

    await client.synthesize("短文本。")

    assert rec.payloads
    for p in rec.payloads:
        assert "ref_audio" in p
        assert "voice" not in p, "克隆模式带 voice 会被上游判 False"
        assert "instruct" not in p, "克隆模式带 instruct 会被上游判 False"


async def test_indextts_自建端点显式带上采样参数():
    """自建端点**能**收 temperature/top_p → 必须显式固定（官方「稳定播音」档）。"""
    rec = _Recorder()
    client = _indextts_client(
        rec,
        speaker="Vivian",
        instruct="温和的中文女声",
        temperature=0.7,
        top_p=0.9,
    )

    await client.synthesize("短文本。")

    assert rec.payloads
    for p in rec.payloads:
        assert p.get("temperature") == 0.7
        assert p.get("top_p") == 0.9


def test_indextts_默认采样参数是_稳定播音_那档():
    """默认就该是 temperature=0.7 / top_p=0.9，而不是上游默认的 top_p=0.95。"""
    client = _indextts_client(_Recorder(), speaker="Vivian", instruct="x")
    assert client.temperature == 0.7
    assert client.top_p == 0.9
    assert client.top_p < 0.95, "top_p 不能等于上游默认（那档偏松，段间会漂）"


def test_indextts_只配_instruct_不配_speaker_直接报错():
    """只给风格控制却没给音色名 → **报错**，不静默降级。

    两条「将就」的路都是坏结果：走克隆路径则 instruct 被静默丢掉
    （以为在控风格、实际没有），走 CustomVoice 路径则 base 模型被上游拒。
    报错是唯一诚实的选择。
    """
    from app.services.tts.indextts import IndexTTSError

    with pytest.raises(IndexTTSError, match="speaker"):
        _indextts_client(_Recorder(), instruct="温和的中文女声")


async def test_indextts_每段都带完全相同的采样参数():
    rec = _Recorder()
    client = _indextts_client(
        rec,
        speaker="Vivian",
        instruct="温和的中文女声",
        temperature=0.7,
        top_p=0.9,
    )
    client.CHUNK_CHARS = 50

    await client.synthesize("这是一段用来触发分段合成的测试文本。" * 6)

    assert len(rec.payloads) >= 2, "这段文本应该被切成多段"
    first = rec.payloads[0]
    for p in rec.payloads[1:]:
        for key in ("voice", "instruct", "temperature", "top_p"):
            assert p.get(key) == first.get(key), f"分段之间 {key} 不一致"
