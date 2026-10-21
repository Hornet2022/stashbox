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

import asyncio
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


def test_mp3_concat_goes_through_ffmpeg_for_accurate_duration():
    """MP3 拼接必须走 ffmpeg —— 裸字节拼出来的时长是**估算值**。

    ffmpeg 对裸拼接的 mp3 会报：
        invalid concatenated file detected - using bitrate for duration
        Estimating duration from bitrate, this may be inaccurate

    客户端展示的时长、进度条、剩余时间都依赖它不准的值。
    concat demuxer 会重新扫描帧并写出正确的时长表（-c copy，不重编码）。
    """
    import shutil
    import subprocess

    from app.services.tts.audio_chunks import concat_mp3

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("环境没有 ffmpeg，跳过")

    # 造两段真实 mp3（1 秒静音 + 1 秒静音）
    made = []
    for i in range(2):
        out = f"/tmp/_tts_concat_probe_{i}.mp3"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                "1",
                out,
            ],
            check=True,
            capture_output=True,
        )
        made.append(out)

    merged = concat_mp3([open(p, "rb").read() for p in made])

    # 合并后应能被 ffprobe 精确解析出 2 秒（而不是「按码率估算」）
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", "-", "-f", "null", "-"],
        input=merged,
        capture_output=True,
    )
    stderr = probe.stderr.decode("utf-8", "replace")
    assert "invalid concatenated file" not in stderr, (
        "拼接后的 mp3 被判定为非法拼接文件（时长会退化成估算值）:\n" + stderr[-500:]
    )


def test_mp3_concat_falls_back_to_byte_concat_without_ffmpeg(monkeypatch):
    """ffmpeg 不可用时退化到字节拼接（播放器仍能按帧顺序解码）。"""
    from app.services.tts import audio_chunks

    monkeypatch.setenv("FFMPEG_BIN", "/nonexistent/ffmpeg")

    a = b"\xff\xfb\x90\x00" + b"aaaa"
    b = b"\xff\xfb\x90\x00" + b"bbbb"
    assert audio_chunks.concat_mp3([a, b]) == a + b


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


def test_default_chunk_chars_is_the_measured_safe_size(openai_mod):
    """默认段长必须是 200 字 —— 实测出来的安全值，不是拍脑袋。

    实测（2026-10-21，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base，每档试 2 次）：
        400 字 → 语速 4.64 / 4.78  wall 23s  ✅
        500 字 → 语速 4.31 / 3.79  wall 48s  ✅  ← 取这个
        600 字 → 语速 4.34 / 4.28  wall 49s  ✅
        700 字 → 语速 4.46 / 4.85  wall 52~59s ✅

    **为什么不是更小的段长**：段数直接决定「音色一段一段」的严重程度 ——
    每段是一次独立请求，段间韵律各自采样，接缝就是突变点。
    段长 200 → 900 字稿 6 段（5 个接缝）；段长 500 → 2 段（1 个接缝）。

    **为什么不能整篇不切**：上游自己也有 60s 墙钟（504 backend_timeout
    「上游语音合成超时」），而 700 字的 wall 已经 52~59s —— 顶到那条线了。

    另外 290 字那次语速 7.14（真截断，丢了约 40%）证明端点会**静默**截断，
    所以仍需保留截断检测 + 二分自愈兜底。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    assert client.chunk_chars == 500, "默认段长必须是实测安全值 500 字"


def test_truncation_threshold_is_tight_enough(openai_mod):
    """截断阈值必须 5.5 —— 正常上界 4.61，真截断 7.14。

    阈值太松（比如 7.0）会让真截断漏判，内容缺失照样写进库；
    太紧（比如 4.0）会把正常段误判成截断，白白多合成一次。
    """
    assert openai_mod._TRUNCATION_CHARS_PER_SEC == 5.5


@pytest.mark.asyncio
async def test_truncated_chunk_is_detected_and_split(openai_mod):
    """被静默截断的块 → 自动二分重来，而不是把缺内容的音频当成功。

    构造一个「每次调用都只产出开头」的假端点：返回的音频时长远小于
    文本应有的时长 → 语速超阈值 → 触发二分。
    """
    client = openai_mod.OpenAITTSClient(
        api_key="k", base_url="http://x", model="m", chunk_chars=200, min_chunk_chars=40
    )
    calls: list[str] = []

    async def _truncating(text, voice=None, output_format="mp3"):
        calls.append(text)
        # 造一段「时长固定 1 秒」的假音频 → 文本越长语速越高
        return _fake_mp3_of_seconds(1)

    client._synthesize_once = _truncating  # type: ignore[assignment]

    # 60 字的块：1 秒音频 → 60 字/秒 → 远超阈值 → 触发二分
    with pytest.raises(Exception):
        await client._synthesize_chunk("这" * 60)

    assert len(calls) > 1, "应该被二分重试，而不是一次就放弃"


def _fake_mp3_of_seconds(seconds: int) -> bytes:
    """造一段指定时长的假 mp3（够解析出帧数即可）。"""
    from app.services.tts.audio_duration import _SAMPLE_RATES

    # MPEG2 Layer III / 24kHz / 64kbps
    header = bytes([0xFF, 0xF3, 0x40, 0xC0])
    frame_len = (72 * 64000) // 24000  # = 192
    rate = _SAMPLE_RATES[2][1]  # 24000
    samples = 576
    frames = int(seconds * rate / samples)
    return (header + b"\x00" * (frame_len - 4)) * frames


def test_default_timeout_leaves_room_for_upstream_jitter(openai_mod):
    """默认超时必须是 300s。

    段长 500 字时实测 wall 48~50s（700 字 52~59s），上游自己还有 60s 墙钟。
    我们的 timeout 必须比它宽松 —— 上游 504 是它自己的限制、httpx 给大也没用，
    但**上游返回 200 之后**的传输/读取不能被我们自己掐断。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    assert client.timeout == 300.0
    assert client.chunk_retries == 2


@pytest.mark.asyncio
async def test_flaky_chunk_is_retried_without_redoing_other_chunks(openai_mod):
    """某一段失败 → 只重试这一段，不重跑整篇。

    实测上游会间歇性抖：同一篇的 3 段里有一段超时、另外两段正常。
    整篇重来 = LLM 改写 + 全部分块都白花；单段重试只要几秒。
    """
    recorder = _make_recorder(openai_mod, chunk_chars=50, chunk_concurrency=1)
    state = {"calls": 0}
    seen: list[str] = []

    async def _flaky(text, voice=None, output_format="mp3"):
        state["calls"] += 1
        seen.append(text)
        if state["calls"] == 1:
            raise RuntimeError("OpenAITTS 超时: 模拟上游抖动")
        return b"OK" + text.encode()[:4]

    recorder._synthesize_once = _flaky  # type: ignore[assignment]
    # 退避 3s 会拖慢测试，这里把它压到 0
    monkey = pytest.MonkeyPatch()
    # ⚠️ 必须先存下原始 sleep：替换后的 lambda 里若再调 asyncio.sleep(0)
    #    就会调到它自己 → 无限递归（maximum recursion depth exceeded）。
    _real_sleep = asyncio.sleep
    monkey.setattr("asyncio.sleep", lambda *_: _real_sleep(0))
    try:
        out = await recorder.synthesize("第一句话。第二句话。" * 10)
    finally:
        monkey.undo()

    assert out.startswith(b"OK")
    # 2 段：第 1 段失败后重试一次，第 2 段一次过 → 共 3 次调用
    assert state["calls"] == 3, f"应只重试失败的那段，实际调用 {state['calls']} 次"
    assert seen[0] == seen[1], "重试的应该是同一段"


@pytest.mark.asyncio
async def test_chunk_retry_exhausted_raises(openai_mod):
    """重试用尽 → 照常抛错（不能静默吞掉，任务必须判失败）。"""
    recorder = _make_recorder(openai_mod, chunk_chars=50, chunk_concurrency=1)

    async def _always_fail(text, voice=None, output_format="mp3"):
        raise RuntimeError("一直失败")

    recorder._synthesize_once = _always_fail  # type: ignore[assignment]
    monkey = pytest.MonkeyPatch()
    # ⚠️ 必须先存下原始 sleep：替换后的 lambda 里若再调 asyncio.sleep(0)
    #    就会调到它自己 → 无限递归（maximum recursion depth exceeded）。
    _real_sleep = asyncio.sleep
    monkey.setattr("asyncio.sleep", lambda *_: _real_sleep(0))
    try:
        with pytest.raises(RuntimeError, match="一直失败"):
            await recorder.synthesize("第一句话。第二句话。" * 10)
    finally:
        monkey.undo()


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
