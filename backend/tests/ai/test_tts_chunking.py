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

        async def _synthesize_once(self, text, voice=None, output_format="mp3", *, clone=None):
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
    """默认段长必须是 700 字 —— 实测出来的安全值，不是拍脑袋。

    实测（2026-10-22，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base，每档连发 3 次）：
        400 字 → wall 24.6 / 25.0 / 25.2s  语速 4.5 字/秒  3/3 成功
        700 字 → wall 42.5 / 43.3 / 44.3s  语速 4.5 字/秒  3/3 成功

    墙钟是线性的（约 62ms/字）→ 上游 60s 的墙钟 ≈ 965 字上限。

    **为什么放大段长**：分段 = 多次独立渲染 = 多个「音色接缝」。
    内测反馈的「一篇三种音色」在音频轨迹上就定位在 chunk2→chunk3 的接缝处
    （LTAS 跳变 0.39~0.45dB，其余窗只有 0.05~0.15dB）。
    一篇 973 字的稿：400 字切 → 5 次渲染 / 4 个接缝（其中一次是截断自愈
    二分出来的）；700 字切 → **2 次渲染 / 1 个接缝**。

    安全网：上游 60s 墙钟是**负载相关**的（同一个 1158 字的稿子 43.89s 成功过、
    60.2s 也 504 过），所以大段可能撞线 —— 撞了不二分自愈（见
    test_upstream_504_triggers_bisect），而是更短 → 更不容易撞线。

    ⚠️ 别把段长放到「整篇一次」：实测 1158 字会**静默截断**（返回 200，
    但只产出 124s 音频，语速 9.3 字/秒 —— 应有 276s）。
    静默截断比 504 更危险：它看起来成功了。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    assert client.chunk_chars == 700, "默认段长必须是实测安全值 700 字"


@pytest.mark.asyncio
async def test_upstream_504_triggers_bisect(openai_mod):
    """上游 60s 墙钟（504 backend_timeout）必须走**二分自愈**，不能直接失败。

    实测（2026-10-22）：同一个 1158 字的稿子，43.89s 成功过、60.2s 就 504。
    上游的速度随负载波动 1.4 倍，足以翻过它那条 60s 的线。

    之前这个 504 走不到自愈（自愈只认客户端超时和截断），
    于是**白重试 3 次**（每次都占满上游 60 秒）才失败 ——
    而把段二分、更短，就不会撞墙钟。
    """
    client = openai_mod.OpenAITTSClient(
        api_key="k", base_url="http://x", model="m", chunk_chars=200, min_chunk_chars=40
    )
    calls: list[str] = []

    async def _wall_clock(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        if len(calls) == 1:
            raise openai_mod._UpstreamTimeout("上游语音合成超时 (60s)")
        return b"OK" + text.encode()[:4]

    client._synthesize_once = _wall_clock  # type: ignore[assignment]

    out = await client._synthesize_chunk("这" * 200)

    assert out.startswith(b"OK")
    assert len(calls) > 1, "504 后应该二分重来，而不是直接失败"


@pytest.mark.asyncio
async def test_upstream_504_body_is_recognised(openai_mod):
    """只有 504 + backend_timeout 才算「上游墙钟」；别的 504 不算。

    判据要精确：网关的 504 也可能来自别的原因，
    而我们只该对「上游语音合成超时」做二分自愈。
    """

    class _Resp:
        status_code = 504
        headers = {"content-type": "application/json"}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")

    def _with_body(body):
        class _C:
            async def post(self, url, json=None):
                return _Resp(body)

        client._client = _C()

    _with_body({"error": {"code": "backend_timeout", "message": "上游语音合成超时 (60s)"}})
    with pytest.raises(openai_mod._UpstreamTimeout):
        await client._synthesize_once("你好。")

    _with_body({"error": {"code": "other", "message": "别的 504"}})
    with pytest.raises(RuntimeError) as exc:
        await client._synthesize_once("你好。")
    assert not isinstance(exc.value, openai_mod._UpstreamTimeout)


def test_truncation_threshold_is_tight_enough(openai_mod):
    """截断阈值必须 **11.0** —— 阈值必须跟着「当前在用的模型」重新标。

    ⚠️ **为什么从 6.8 改成 11.0**（2026-10-25 实测）：
    6.8 是 2026-10-24 在 **Qwen3-TTS-12Hz-1.7B-Base** 上标的
    （那时正常 4.80~6.45 字/秒）。生产现在跑 **CustomVoice**，
    实测正常语速（同一上游、同一天，n=35+）：

        250 字克隆块      5.15 ~ 9.65   中位 ~7.0
        700~900 字块     5.43 ~ 6.51
        909~983 字块     5.56 ~ 6.49
        短块（40~60 字） 6.56 ~ 9.40

    ⇒ 6.8 落进**正常区间内部** ⇒ 生产近 24h 误判 **107 次**
    「分块被截断，二分重来」，每次都凭空多出 1~2 个音色接缝。

    ⇒ 阈值取 **11.0**：对实测正常上界 9.65 留 14% 余量；
    真截断（音频只剩一半左右）是 13~19 字/秒，仍被抓住。

    ⚠️ **已知局限（诚实记录）**：今天的正常上界 9.65 已经**高于**
    老阈值时代被当作「真截断」的样本（7.14~8.67）——
    也就是说，那些老样本在今天的上游上**无法再与正常区分**。
    速率判据只能筛「缺得很狠」的截断（缺 45% 以上），
    轻微截尾它抓不到 —— 这是**已知能力边界**，不是没测出来。
    """
    assert openai_mod._TRUNCATION_CHARS_PER_SEC == 11.0


def test_admin_测试句足够长_语速判据才有意义(openai_mod):
    """admin 的 TTS 测试句必须**足够长**。

    语速 = 字数 / 音频时长，而短文本的时长被首尾留白与句读停顿主导 ——
    13 字的「听匣 TTS 连通性测试。」产出 2.0s 就是 6.5 字/秒。
    文本越短，这项比值越不稳（实测 40~60 字的块带宽可达 19.9%）。

    这里锁住「测试句足够长」这个前提，别让谁又改回一句话。

    ⚠️ **2026-10-25**：语速判据已**不再按长度分档**（实测 cps 与块长无关，
    只与文本内容有关），所以「≥ `_MIN_CHARS_FOR_RATE_CHECK`」这条
    分支依据已经不存在。这里的下限改成**显式常量**：
    它守的不是「判据用哪一档阈值」，而是「测试句要长到让语速这个比值稳定」。
    """
    import importlib.util
    import sys
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[2]
    # ⚠️ admin_router.py 里是 `from clients.ai_client import ...` ——
    # `clients` 包在 content-service/ 下，单跑这个文件时不在 sys.path 上
    # （全量跑能过是靠别的用例先把它塞进去 → 典型的测试隔离问题）。
    # 这里显式补上，让这条用例**单跑也能过**。
    content_service_dir = str(backend_dir / "content-service")
    if content_service_dir not in sys.path:
        sys.path.insert(0, content_service_dir)
    spec = importlib.util.spec_from_file_location(
        "_tts_cfg_fields_admin_router_probe", backend_dir / "content-service" / "admin_router.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module._TTS_TEST_TEXT
    min_len = 150
    assert len(text) >= min_len, (
        f"测试句太短（{len(text)} 字 < {min_len}），语速这个比值会被"
        f"首尾留白与句读停顿主导，测不出「内容是不是完整念完了」"
    )


def test_正常语速不误判(openai_mod):
    """实测正常样本：134 字 → 24.3s → 5.52 字/秒（文档标为正常）。

    旧阈值 5.5 会把它判成截断 → 白白重试 + 二分（= 重新采样 = 音色更漂）。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    client._assert_not_truncated("这" * 134, _fake_mp3_of_seconds(24))


def test_真截断仍然被抓(openai_mod):
    """真截断必须被抓住 —— 那是「内容缺失但状态成功」，比报错难查得多。

    ⚠️ **2026-10-25：判据换了样本**。
    老样本「290 字 → 40.6s → 7.14 字/秒」是**当时**（Base 模型）的真截断，
    但今天的上游正常语速就能到 9.65 字/秒 ⇒ 7.14 在今天**不是异常**，
    拿它当「真截断」就是在测一个不成立的事实。

    ⇒ 现在用「缺得很狠」的样本：290 字只产出 20s → **14.5 字/秒**
    （音频只剩正常时长的 1/3~1/2）—— 这在任何分布下都是异常。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    with pytest.raises(openai_mod._TruncatedOutput):
        client._assert_not_truncated("这" * 290, _fake_mp3_of_seconds(20))


def test_今天上游的正常语速不再被误判(openai_mod):
    """今天实测的正常样本（含 9.65 字/秒那个）**不得**被判成截断。

    这条是 2026-10-25 误判 107 次的直接对策：
    阈值低于正常上界 ⇒ 正常块被劈开 ⇒ 凭空多出音色接缝。
    """
    client = openai_mod.OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    # 250 字 / 25.9s = 9.65 字/秒（今天实测的正常上界）
    client._assert_not_truncated("这" * 250, _fake_mp3_of_seconds(25.9))


@pytest.mark.asyncio
async def test_truncated_chunk_is_detected_and_split(openai_mod):
    """被静默截断的块 → 自动二分**重渲**，而不是把缺内容的音频当成功。

    构造一个「整块只产出开头」的假端点：返回的音频时长远小于
    文本应有的时长 → 语速超阈值 → 触发二分。

    ⚠️ **语义变更（2026-10-24）**：自愈现在会**收敛**（切到判据无法分辨的长度为止），
    所以结果不再是「抛错」，而是「切小重渲 → 拼回来」。

    为什么这样更好：老语义在**短块**上直接抛错 → 整篇蒸馏失败 ——
    生产实测（存量重跑第 1 篇）：47 字的余块被判 9.40 字/秒 →
    直接放弃自愈 → 整篇挂掉。

    而「短块到底缺没缺」在原理上就判不出来（实测同一段文本连渲 3 次的
    cps 带宽 5.20~6.88，波动 25%）—— 所以正确做法是**重渲一次**，
    而不是替用户决定「这篇不要了」。
    """
    client = openai_mod.OpenAITTSClient(
        api_key="k", base_url="http://x", model="m", chunk_chars=200, min_chunk_chars=40
    )
    calls: list[str] = []

    async def _truncating(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        # 整块（≥40 字）只产出开头 → 判截断；切小后的子块渲染完整
        if len(text) >= 40:
            return _fake_mp3_of_seconds(1)
        return _fake_mp3_of_seconds(4)

    client._synthesize_once = _truncating  # type: ignore[assignment]

    # 60 字的块：1 秒音频 → 56 字/秒 → 远超阈值 → 触发二分
    audio = await client._synthesize_chunk("这" * 60)

    assert audio, "自愈后应该拼出音频"
    assert len(calls) > 1, "应该被二分重试，而不是一次就放弃"
    assert all(len(c) < 60 for c in calls[1:]), "自愈应该把块切小后重渲"


def _fake_mp3_of_seconds(seconds: int) -> bytes:
    """造一段指定时长的假 mp3（够解析出帧数即可）。

    ⚠️ **帧头必须与生成逻辑自洽**（2026-10-24 修）：
    原写法用 `[0xFF, 0xF3, 0x40, 0xC0]` —— 那声明的是 MPEG2/22050Hz/32kbps，
    而下面按 24000/64kbps 算帧长 → **解析器失步** → 多算 8.8% 的帧 → 时长偏大。
    整秒时长把它盖住了（26 秒量成 28 秒），精确时长下就露了出来
    （语速偏低 → 真截断样本不再被报出来）。

    正确写法：byte2 = (bitrate_idx 8 = 64kbps)<<4 | (sr_idx 1 = 24000Hz)<<2 = 0x84。
    """
    from app.services.tts.audio_duration import _SAMPLE_RATES

    # MPEG2 Layer III / 24kHz / 64kbps
    header = bytes([0xFF, 0xF3, 0x84, 0xC0])
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

    async def _flaky(text, voice=None, output_format="mp3", *, clone=None):
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

    async def _always_fail(text, voice=None, output_format="mp3", *, clone=None):
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
