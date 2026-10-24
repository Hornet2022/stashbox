"""克隆（零样本）路径的契约测试：openai provider 必须把**同一份**参考音带进每一块。

## 这组测试守的是什么

2026-10-24 实测闭合的根因：托管端点（tertu.cn / Qwen3-TTS-12Hz-1.7B-Base）
**不认 `voice` 这个说话人名** —— 同一段文本、同一个 `voice=uncle_fu` 连发 3 次，
基频中位数 165.0 / 188.5 / 105.3 Hz（三个不同的人）。

我们按 700 字分块 → 每块一次请求 → **每块一个人** → 用户听到「分段音色差异」。

修法只有一个：**让零样本克隆真的生效**（同一份 ref_audio 连发 3 次 →
217.9 / 219.3 / 224.7 Hz，±1.6%）。所以这里钉死三件事：

1. `ref_audio` 必须是 **data: URL**。
   裸 base64 上游不认 —— 表现是 **504 超时**（请求合法、上游读不出音频 →
   干等 60s 墙钟），比 400 更难查。
2. `ref_text` 必须同时给（上游缺了直接 400）。
3. **每一块**都必须带**逐字节相同**的参考音频。
   只要有一块带的是别的东西，那一块就是另一个人说话。

## 为什么是契约测试而不是直接测基频

基频要真上游 + ffmpeg + 几十秒墙钟，不适合进 CI。
这里用 mock 钉住**机制**（每块带的参考音是同一份、形态是对的），
把「音高是否真的一致」交给 `backend/scripts/check_tts_voice_consistency.py`
（实测脚本，判据：各块基频中位数差异 < 5%）。

⚠️ 那个 5% 不是拍脑袋：实测**同文 3 次**的噪声底在 65s 块长下是 **1.6%**
（7s 短文本下是 7.8% —— 那是度量噪声，不是音色漂移），
所以 5% ≈ 3 倍噪声底，既有区分度又不会被上游的正常抖动误伤。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts import openai as openai_module  # noqa: E402
from app.services.tts.openai import OpenAITTSClient  # noqa: E402
from app.services.tts.ref_audio import (  # noqa: E402
    RefAudioError,
    detect_audio_mime,
    load_ref_audio_bytes,
    load_ref_audio_data_url,
    to_data_url,
)

# 一段「合法 wav」：RIFF 头 + 填充。只需要魔数对，字节数过下限即可。
FAKE_WAV = b"RIFF" + b"\x00" * 4096
REF_TEXT = "今天天气不错，我们一起来看看这条新闻讲了什么内容。"


class _Resp:
    """最小 httpx.Response 替身。"""

    def __init__(self, status_code=200, content=b"", headers=None, payload=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "audio/mpeg"}
        self._payload = payload
        self.text = "" if payload is None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _reset_circuit():
    """熔断状态是模块级全局的 —— 用例之间必须隔离。"""
    openai_module._circuit_state["consecutive_failures"] = 0
    openai_module._circuit_state["opened_at"] = 0.0
    yield
    openai_module._circuit_state["consecutive_failures"] = 0
    openai_module._circuit_state["opened_at"] = 0.0


@pytest.fixture
def concat_stub(monkeypatch):
    """把拼接换成「直接首尾相接」。

    我们要验的是**请求体**（每块带了什么），不是音频拼接；
    真拼接要合法 mp3 + ffmpeg crossfade，会让这个测试又慢又脆。
    """
    monkeypatch.setattr(
        openai_module,
        "concat_audio",
        lambda pieces, gap_sec=0.3: b"".join(pieces),
    )


def _client(chunk_chars: int = 20, **kwargs) -> OpenAITTSClient:
    return OpenAITTSClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="Qwen3-TTS-12Hz-1.7B-Base",
        voice="uncle_fu",
        chunk_chars=chunk_chars,
        chunk_retries=0,
        **kwargs,
    )


def _install_post(client, handler):
    """替换 `client._client.post`，返回记录到的请求体列表。"""
    bodies: list[dict] = []

    async def _post(url, json=None, **kw):
        bodies.append(json)
        return handler(json or {})

    client._client.post = _post  # type: ignore[method-assign]
    return bodies


# ==========================================================================
# 1. 参考音频模块（ref_audio.py）
# ==========================================================================


def test_magic_bytes_win_over_suffix():
    """魔数比扩展名可信 —— 别人塞进来的 `.mp3` 可能其实是 wav。"""
    assert detect_audio_mime(FAKE_WAV, "x.wav") == "audio/wav"
    assert detect_audio_mime(b"ID3\x04\x00" + b"\x00" * 100, "x.wav") == "audio/mpeg"
    assert detect_audio_mime(b"\xff\xfb\x90\x00" + b"\x00" * 100, "x.wav") == "audio/mpeg"
    assert detect_audio_mime(b"OggS" + b"\x00" * 100, "x.wav") == "audio/ogg"
    assert detect_audio_mime(b"fLaC" + b"\x00" * 100, "x.wav") == "audio/flac"


def test_suffix_is_only_a_fallback():
    """魔数认不出来才看扩展名；都没有就兜底 audio/wav。"""
    assert detect_audio_mime(b"\x00" * 100, "voice.mp3") == "audio/mpeg"
    assert detect_audio_mime(b"\x00" * 100, "voice.m4a") == "audio/mp4"
    assert detect_audio_mime(b"\x00" * 100, "voice.bin") == "audio/wav"


def test_data_url_roundtrip():
    """编出来的 data URL 必须能解回原字节 —— 否则克隆源被悄悄改过。"""
    url = to_data_url(FAKE_WAV, "audio/wav")
    assert url.startswith("data:audio/wav;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw == FAKE_WAV


async def test_load_from_local_file(tmp_path):
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    assert await load_ref_audio_bytes(str(p)) == FAKE_WAV
    url = await load_ref_audio_data_url(str(p))
    assert url.startswith("data:audio/wav;base64,")


async def test_load_from_data_url():
    url = to_data_url(FAKE_WAV, "audio/wav")
    assert await load_ref_audio_bytes(url) == FAKE_WAV
    # 已经是 data URL 就**别再编一次**（base64 套 base64 = 上游解不出来）
    again = await load_ref_audio_data_url(url)
    assert base64.b64decode(again.split(",", 1)[1]) == FAKE_WAV


async def test_too_small_is_rejected():
    """太小的文件不是参考音频 —— 要早点报错，别让上游干等。"""
    with pytest.raises(RefAudioError):
        await load_ref_audio_bytes("/nonexistent/ref.wav")
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"RIFF")
        tiny = f.name
    with pytest.raises(RefAudioError):
        await load_ref_audio_bytes(tiny)


# ==========================================================================
# 2. openai provider 的克隆透传（契约）
# ==========================================================================


async def test_ref_audio_is_sent_as_data_url_not_raw_base64(tmp_path, concat_stub):
    """**裸 base64 上游不认**（会 504 超时，不是 400）—— 必须编成 data URL。"""
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    client = _client(chunk_chars=1000)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize("一段短文本。", ref_audio=str(p), ref_text=REF_TEXT)

    assert len(bodies) == 1
    sent = bodies[0]["ref_audio"]
    assert sent.startswith(
        "data:audio/wav;base64,"
    ), f"ref_audio 不是 data URL：{sent[:40]}… —— 上游会 504 超时"
    # 不能是裸 base64：data URL 的 base64 部分解出来必须是原始 wav
    raw = base64.b64decode(sent.split(",", 1)[1])
    assert raw == FAKE_WAV


async def test_every_chunk_carries_identical_ref_audio(tmp_path, concat_stub):
    """**每一块**都要带逐字节相同的参考音 —— 少一块就是那一个人换了。"""
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    client = _client(chunk_chars=8)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize(
        "第一句话。第二句话。第三句话。第四句话。第五句话。",
        ref_audio=str(p),
        ref_text=REF_TEXT,
    )

    assert len(bodies) >= 3, "这段文本应该切出多块"
    refs = {b["ref_audio"] for b in bodies}
    assert len(refs) == 1, f"各块带的参考音不一致（{len(refs)} 种）—— 那就是几个不同的人"
    assert all(b["ref_text"] == REF_TEXT for b in bodies)


async def test_missing_ref_text_fails_before_any_request(tmp_path, concat_stub):
    """上游缺 ref_text 会 400；本地就要拦下 —— 省一次 60s 的上游请求。"""
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    client = _client(chunk_chars=1000)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    with pytest.raises(ValueError, match="ref_text"):
        await client.synthesize("一段短文本。", ref_audio=str(p))

    assert bodies == [], "缺 ref_text 时不该发任何请求"


async def test_missing_ref_audio_file_raises_clear_error(concat_stub):
    """参考音拿不到 → 明确报错，而不是让上游 504。"""
    client = _client(chunk_chars=1000)
    _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    with pytest.raises(RuntimeError, match="克隆参考音频"):
        await client.synthesize(
            "一段短文本。",
            ref_audio="/nonexistent/ref.wav",
            ref_text=REF_TEXT,
        )


async def test_without_clone_no_ref_fields_are_sent(concat_stub):
    """老路径不变：不传 ref_audio 时，请求体里不该出现 ref_* 字段。

    （上游对未知字段是**静默丢弃**的 —— 「传了没报错」不能当成「支持」，
    所以这里断言的是「不该传时真的没传」。）
    """
    client = _client(chunk_chars=1000)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize("一段短文本。")

    assert "ref_audio" not in bodies[0]
    assert "ref_text" not in bodies[0]


async def test_bisect_selfheal_keeps_the_same_ref_audio(tmp_path, concat_stub):
    """上游 504 触发的二分自愈，子块也必须带**同一份**参考音。

    自愈是为了「更短 → 更不容易撞墙钟」，不是为了换个人 ——
    如果子块不带参考音，自愈出来的接缝又是「另一个人」。
    """
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    client = _client(chunk_chars=40, min_chunk_chars=5)
    calls: list[dict] = []

    def _handler(body):
        calls.append(body)
        # 第一次 504（上游墙钟）→ 触发二分；之后都成功
        if len(calls) == 1:
            return _Resp(
                504,
                b"",
                headers={"content-type": "application/json"},
                payload={"error": {"code": "backend_timeout"}},
            )
        return _Resp(200, b"x" * 2000)

    _install_post(client, _handler)

    await client.synthesize(
        "第一句话。第二句话。第三句话。第四句话。第五句话。",
        ref_audio=str(p),
        ref_text=REF_TEXT,
    )

    assert len(calls) >= 2, "应该发生了一次二分自愈"
    refs = {c["ref_audio"] for c in calls}
    assert len(refs) == 1, "二分出来的子块带了不同的参考音"


async def test_clone_and_voice_are_both_sent(concat_stub, tmp_path):
    """克隆模式下 voice 照常带 —— 上游会忽略它（实测非法名也 200）。

    这里钉住「不要因为『voice 在克隆下无意义』就顺手改掉请求体形状」：
    那是行为变更，得有独立的理由和独立的测试。
    """
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    client = _client(chunk_chars=1000)
    bodies = _install_post(client, lambda body: _Resp(200, b"x" * 2000))

    await client.synthesize("一段短文本。", ref_audio=str(p), ref_text=REF_TEXT)

    assert bodies[0]["voice"] == "uncle_fu"
    assert bodies[0]["ref_audio"].startswith("data:")


# ==========================================================================
# 3. 克隆路径的块长单独算（上游克隆慢 1.66 倍 → 更容易撞 60s 墙钟）
# ==========================================================================


async def test_clone_uses_smaller_chunks_than_plain_path(tmp_path, concat_stub):
    """克隆时块长必须**另算**（默认 250 字），不能沿用普通路径的 700。

    实测（2026-10-24，tertu.cn）：同样 700 字，
        不克隆 → 音频 185.8s（3.77 字/秒）
        克隆   → 音频 112.2s（6.24 字/秒）—— **慢 1.66 倍**
    上游克隆时要额外处理参考音频 → 更容易撞它 60s 的墙钟，
    而撞墙钟的后果是**静默截断**（HTTP 200 + 音频变短）→ 内容缺失。
    """
    p = tmp_path / "ref.wav"
    p.write_bytes(FAKE_WAV)
    text = "这是一段用来测试分块的中文文本。" * 20  # 280 字

    plain = _client(chunk_chars=700)
    plain_bodies = _install_post(plain, lambda body: _Resp(200, b"x" * 2000))
    await plain.synthesize(text)
    plain_chunks = len(plain_bodies)

    cloned = _client(chunk_chars=700)
    clone_bodies = _install_post(cloned, lambda body: _Resp(200, b"x" * 2000))
    await cloned.synthesize(text, ref_audio=str(p), ref_text=REF_TEXT)
    clone_chunks = len(clone_bodies)

    assert plain_chunks == 1, "280 字在普通路径（700）下应该是 1 块"
    assert clone_chunks > plain_chunks, (
        f"克隆路径用了和普通路径一样的块长（{clone_chunks} 块）—— "
        f"克隆慢 1.66 倍，更容易撞上游 60s 墙钟 → 静默截断 → 内容缺失"
    )
    assert all(
        len(b["input"]) <= 250 for b in clone_bodies
    ), "克隆块长应 ≤ 250 字（实测 250 字 → 音频 40~44s，离 60s 墙钟有 35% 余量）"


def test_short_text_is_not_flagged_as_truncated():
    """短文本的高语速**不能**判成截断 —— 会把二分自愈一路带到 8 字。

    实测：上游对 8 个字返回 1.0s 音频 → 8.00 字/秒 → 超阈值。
    按老判据这会触发二分 → 700→350→175→…→8 每一次都超阈值 →
    一路钻到 min_chunk_chars 仍报错 → **整篇蒸馏失败**。
    """
    client = _client()
    # 8 个字 / 1 秒 → 8 字/秒（超阈值），但只缺了 8/4.0=2.0s 期望里的一半边缘
    client._assert_not_truncated("八个字而已哦", _fake_mp3_of_seconds(1))
    # 严重缺失（1 秒 vs 期望 5 秒）仍然要判成截断
    with pytest.raises(openai_module._TruncatedOutput):
        client._assert_not_truncated("这" * 20, _fake_mp3_of_seconds(1))


def test_long_text_truncation_is_still_detected():
    """长文本的截断检测不能因为上面的放宽而失效。"""
    client = _client()
    with pytest.raises(openai_module._TruncatedOutput):
        client._assert_not_truncated("这" * 290, _fake_mp3_of_seconds(40))


# ---------------------------------------------------------------------------
# 短块判据的长度自适应（2026-10-24 生产实测标定）
# ---------------------------------------------------------------------------
#
# 生产事故：存量重跑第 1 篇失败于
#     「OpenAITTS 输出疑似被截断：47 字只产出 5.0s 音频（9.40 字/秒，正常应 < 6.5）」
#
# 根因是**两个阈值打架**：
#   · 语速判据从 40 字就启用（`_MIN_CHARS_FOR_RATE_CHECK`）
#   · 二分自愈的最小粒度是 60 字（`min_chunk_chars`）
# ⇒ 40 ≤ 字数 < 60 的块一旦超阈值就**必然抛错、无法自愈** → 整篇蒸馏失败。
# 而 250 字分块的**最后一块**常常只剩 40~60 字 —— 那是**每篇都有**的余块。
#
# 实测标定（同一段文本、同一份参考音，连渲 3 次）：
#     47 字 → cps 5.88~6.71（波动 14.3%）
#     52 字 → cps 5.20~6.50（波动 25.0%）
#     55 字 → cps 5.50~6.88（波动 25.0%）
#    114 字 → cps 5.70~6.71（波动 17.6%）
#    250 字 → cps 5.72~6.25（波动  4.5%）
# ⇒ 阈值 6.5 **就落在短块的噪声带里** → 正常渲染被判成截断。


@pytest.mark.parametrize(
    ("chars", "seconds"),
    [
        (47, 7),  # 实测 5.88~6.71 cps
        (47, 8),
        (52, 8),  # 实测 5.20~6.50 cps
        (55, 8),  # 实测 5.50~6.88 cps
        (114, 17),  # 实测 5.70~6.71 cps
    ],
)
def test_短块在实测噪声带内不误判(chars, seconds):
    """短块的语速落在**实测噪声带**里时，不能判成截断。

    这是生产上真实触发的那一类：47 字的块被报成 9.40 字/秒 → 整篇挂掉。
    """
    client = _client()
    client._assert_not_truncated("这" * chars, _fake_mp3_of_seconds(seconds))


def test_生产实测那条会被判出来():
    """生产事故原样复现：47 字 / 5.0s（9.40 字/秒）→ **要判成异常**。

    47 字的自然带是 7.0~8.0s（5.88~6.71 cps），5.0s 明显在带外 →
    它**确实**是个异常，不该被放过。

    注意：判出来只是第一步 —— 真正拖死整篇的是「判出来之后没法自愈」，
    那由下面那条用例守住。
    """
    client = _client()
    with pytest.raises(openai_module._TruncatedOutput):
        client._assert_not_truncated("这" * 47, _fake_mp3_of_seconds(5))


@pytest.mark.asyncio
async def test_生产实测那条不会拖死整篇():
    """**死区修复**：47 字的块被判异常后，必须能自愈重渲，而不是整篇挂掉。

    老写法 `len(text) <= min_chunk_chars(60) → raise` 让短块一旦误判就
    **直接放弃自愈** → 整篇蒸馏失败（生产实测：第 1 篇就挂，靠 Arq 重试才救回来）。

    这里模拟真实上游：第一次渲染偏短（5.0s）→ 判异常 → 二分 →
    子块重新渲染（正常长度）→ 通过 → 整块合成成功。
    """
    client = _client(min_chunk_chars=60)
    calls: list[str] = []

    async def _sometimes_short(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        # 整块（47 字）偏短；子块正常
        if len(text) > 30:
            return _fake_mp3_of_seconds(5)
        return _fake_mp3_of_seconds(4)

    client._synthesize_once = _sometimes_short  # type: ignore[assignment]

    audio = await client._synthesize_chunk("这" * 47)

    assert audio, "自愈后应该合成出音频"
    assert len(calls) > 1, (
        "47 字的块比 min_chunk_chars(60) 还小，但**仍应触发二分自愈** —— "
        "老写法在这里直接放弃，导致整篇蒸馏失败"
    )
    assert all(len(c) < 47 for c in calls[1:]), "自愈应该把块切小后重渲"


def test_短块的明确缺失仍然被抓():
    """短块只认「明显缺失」—— 不能把真截断一起放过。"""
    client = _client()
    with pytest.raises(openai_module._TruncatedOutput):
        # 20 字 → 1 秒 → 18.7 字/秒，远超短文本阈值 8.0
        client._assert_not_truncated("这" * 20, _fake_mp3_of_seconds(1))


@pytest.mark.asyncio
async def test_自愈切不动时就停():
    """自愈的地板是「切不动了」，不是 min_chunk_chars。

    ⚠️ 地板管的是「**被切的文本**」：文本比 `_MIN_SPLIT_CHARS` 还小就不切了。
    切**出来**的块可以更小（硬切会留下 1 字的余块）—— 那不是问题。
    """
    client = _client(min_chunk_chars=60)
    calls: list[str] = []

    async def _short_audio(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        return _fake_mp3_of_seconds(1)

    client._synthesize_once = _short_audio  # type: ignore[assignment]

    with pytest.raises(Exception):
        await client._synthesize_chunk("这" * 47)

    # 递归必须收敛（深度上限 6 ⇒ 最多 2^6 个叶子），不能无限切
    assert len(calls) <= 2 ** (
        openai_module._MAX_SPLIT_DEPTH + 1
    ), f"自愈切了 {len(calls)} 次，没有收敛"
    # 且确实切到了地板以下（说明是被地板挡住的，不是被 min_chunk_chars 挡住的）
    assert min(len(c) for c in calls) < openai_module._MIN_SPLIT_CHARS, (
        "递归应该一路切到地板以下才停 —— "
        "否则说明它是被 min_chunk_chars 挡住的（老行为：短块直接放弃自愈）"
    )


def _fake_mp3_of_seconds(seconds: int) -> bytes:
    """造一段指定时长的假 mp3（够解析出帧数即可）。"""
    from app.services.tts.audio_duration import _SAMPLE_RATES

    header = bytes([0xFF, 0xF3, 0x40, 0xC0])
    frame_len = (72 * 64000) // 24000  # = 192
    rate = _SAMPLE_RATES[2][1]  # 24000
    samples = 576
    frames = int(seconds * rate / samples)
    return (header + b"\x00" * (frame_len - 4)) * frames
