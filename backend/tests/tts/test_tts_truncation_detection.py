"""截断检测契约：阈值分档 + 同文本重渲优先（2026-11-27 生产事故驱动）。

## 事故

2026-11-27 生产重生成 4 篇文章，**2 篇的音频整块丢内容**
（ASR 逐段对照：稿里 ≈590 字在音频里消失）。

用生产日志逐块算语速（不依赖 ASR）定位到：

    art_a7ebe143 chunk1  690字 → 9.44 字/秒（同篇其他块 4.33~4.70）
    art_d00811fb chunk2  679字 → 8.14 字/秒（同篇其他块 4.36~5.12）

⇒ **旧阈值 11.0 把真截断放过去了**（8.14 / 9.44 < 11.0）。

## 为什么旧阈值这么松

它的基线（"正常带 5.15~9.65"）是 2026-10-25 标的，
而那份样本里**混进了被截断的样本** —— 同一个 250 字块两次实测 5.96 与 9.65，
而 9.65 正是"音频只剩一半"的语速签名（≈5.0×2）。
⇒ **坏统计量把防线抬高**：阈值取在污染后的上界之上 ⇒ 真截断反而无人守。

## 重标定

干净样本（**预设路径** clone=off，字数取自生产稿件真实切分，
时长由「响应字节 / 上游码率」推得 —— 码率经实测时长校验 ±3%；
样本 = 4 篇 × 4 块 + 11-26 标定 7 块 + 重放 9 次 + 二分 6 块，n=33）：

    健康带 **4.07 ~ 5.72 字/秒**（其中 5.72 这个样本的标签**存疑**：它出自
    「同一段文本重渲」实验，而该实验做完后稿件被重生成改写过 ⇒ 拿新稿对齐旧渲染，
    相似度失真 ⇒ 那次标注实验已作废；5.72 若实为轻截断，健康上界就是 5.49、
    阈值余量 +9.3%。两种情况下 6.0 都成立 ⇒ 这里只钉**已确认**的健康样本）
    截断签名 **5.81**（丢 26%）/ **8.14** / **9.44** / **19.97**（丢 ~50%~75%）

⇒ 预设路径长块阈值 **6.0**（高于实测的"丢一半"级截断 19%~24%）；
   克隆路径沿用 **11.0**（带更宽 + 该路径已停用）；
   短块沿用 **11.0**（语速被首尾留白主导）。

## 修复的第二半：检出之后怎么办

上游的短响应是**偶发**的 —— 同一段文本重渲 3 次全部拿到完整音频
（4.47 / 4.81 / 4.11 字/秒，而出事那次是 9.44）。
⇒ 自愈第一步改成**同文本重渲**（命中率高，且**不新增接缝**），
   仍截断才切小（二分）。克隆路径不做同文本重渲（重采样说话人会换人）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts import openai as openai_module  # noqa: E402
from app.services.tts.openai import OpenAITTSClient  # noqa: E402


def _client(**kwargs) -> OpenAITTSClient:
    return OpenAITTSClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="Qwen3-TTS-12Hz-1.7B-CustomVoice",
        voice="uncle_fu",
        chunk_retries=0,
        **kwargs,
    )


def _fake_mp3_of_seconds(seconds: float) -> bytes:
    """造一段指定时长的假 mp3（帧头与解析器自洽，见 tests/ai/test_tts_chunking.py）。"""
    from app.services.tts.audio_duration import _SAMPLE_RATES

    header = bytes([0xFF, 0xF3, 0x84, 0xC0])
    frame_len = (72 * 64000) // 24000  # = 192
    rate = _SAMPLE_RATES[2][1]  # 24000
    samples = 576
    frames = int(seconds * rate / samples)
    return (header + b"\x00" * (frame_len - 4)) * frames


def _clone_spec():
    return openai_module._CloneSpec(
        source="default", data_url="data:audio/mp3;base64,AA", text="参考"
    )


# ── 生产实测样本（2026-11-27）：(字数, 音频秒) ───────────────────────────
# 健康块：4 篇 × 4 块（11-27 重生成）+ 11-26 标定 7 块 + 重放 5 次
_HEALTHY_SAMPLES = [
    # art_30fb7dca（4 块）
    (695, 150.6),
    (688, 144.3),
    (647, 148.4),
    (171, 34.3),
    # art_a7ebe143（4 块，chunk1 是被截断的那块，不在此列）
    (680, 151.8),
    (692, 147.3),
    (195, 45.0),
    # art_d00811fb（4 块，chunk2 是被截断的那块）
    (670, 130.9),
    (577, 132.3),
    (294, 60.1),
    # art_dc2c2e32（4 块）
    (697, 155.4),
    (669, 152.0),
    (649, 151.2),
    (289, 70.6),
    # 11-26 标定（700 / 1000 块长）
    (674, 156.7),
    (655, 147.5),
    (645, 131.6),
    (368, 78.0),
    (986, 226.7),
    (990, 216.3),
    (368, 80.6),
    # 重放（同一段文本重渲 3 次 + 二分两半）
    (690, 152.6),
    (690, 143.2),
    (690, 167.5),
    (345, 81.7),
    (345, 81.4),
]

# 被截断的块（生产实测）：(字数, 音频秒, 说明)
_TRUNCATED_SAMPLES = [
    (690, 73.1, "art_a7ebe143 chunk1：690 字只产出 73.1s ⇒ 9.44 字/秒"),
    (679, 83.4, "art_d00811fb chunk2：679 字只产出 83.4s ⇒ 8.14 字/秒"),
    (
        679,
        34.0,
        "重放实验第 2 次渲染：679 字只产出 34.0s ⇒ 19.97 字/秒"
        "（**同文本重渲也会中招** ⇒ 截断会复发，不能靠「重渲一次就完事」）",
    ),
]

# 已知能力边界：丢 26% 的轻截断（1484 字块，11-26 实测）语速 5.81，
# 落在健康带（≤5.12）与严格阈值（6.0）之间 ⇒ **抓不到**。
# 这里显式钉住这条边界：谁想收紧到能抓它，必须同时更新契约与守卫。
_MILD_TRUNCATION_SAMPLE = (1484, 255.3)


def test_阈值分档_预设长块用严格档():
    """预设路径长块 → 6.0；克隆路径 / 短块 → 11.0。"""
    client = _client()
    assert client._truncation_cps_limit("这" * 190, None) == 6.0
    assert client._truncation_cps_limit("这" * 190, _clone_spec()) == 11.0
    assert client._truncation_cps_limit("这" * 149, None) == 11.0
    assert client._truncation_cps_limit("这" * 150, None) == 6.0


def test_生产实测健康块全部不误判():
    """n=26 的真实健康块 ⇒ 一条都不许被判成截断。"""
    client = _client()
    for chars, seconds in _HEALTHY_SAMPLES:
        client._assert_not_truncated("这" * chars, _fake_mp3_of_seconds(seconds))


def test_生产实测截断块必须被抓出来():
    """两条真实截断（≈丢一半内容）必须报出来 —— 否则内容缺陷照旧上线。"""
    client = _client()
    for chars, seconds, why in _TRUNCATED_SAMPLES:
        with pytest.raises(openai_module._TruncatedOutput):
            client._assert_not_truncated("这" * chars, _fake_mp3_of_seconds(seconds))
        assert why  # 保留样本出处，便于排查时回溯


def test_轻截断是已知能力边界():
    """丢 26% 的轻截断（5.81 字/秒）**抓不到** —— 这是已知边界，显式钉住。"""
    client = _client()
    chars, seconds = _MILD_TRUNCATION_SAMPLE
    client._assert_not_truncated("这" * chars, _fake_mp3_of_seconds(seconds))


@pytest.mark.asyncio
async def test_检出截断后先同文本重渲():
    """自愈第一步是**同文本重渲**（上游短响应是偶发的，且不新增接缝）。"""
    client = _client()
    calls: list[str] = []

    async def _sometimes_short(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        if len(calls) == 1:  # 第一次只产出开头 ⇒ 判截断
            return _fake_mp3_of_seconds(1)
        return _fake_mp3_of_seconds(60)  # 重渲即完整

    client._synthesize_once = _sometimes_short  # type: ignore[assignment]

    audio = await client._synthesize_chunk("这" * 190)

    assert audio, "重渲成功后应当返回音频"
    assert len(calls) == 2, f"同文本重渲应当一次就好，不该切小（实际调用 {len(calls)} 次）"
    assert calls[1] == calls[0], "重渲的必须是**同一段文本**（不切小、不换文本）"


@pytest.mark.asyncio
async def test_同文本重渲仍失败才切小():
    """重渲仍截断 ⇒ 回落既有二分自愈（切小后渲染完整 ⇒ 拼回来）。"""
    client = _client()
    calls: list[str] = []

    async def _always_short(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        if len(text) >= 100:  # 整块与同文本重渲都只产出开头
            return _fake_mp3_of_seconds(1)
        return _fake_mp3_of_seconds(60)

    client._synthesize_once = _always_short  # type: ignore[assignment]

    audio = await client._synthesize_chunk("这" * 190)

    assert audio, "切小自愈后应当拼出音频"
    assert calls[1] == calls[0], "第一步是同文本重渲"
    assert all(len(c) < 190 for c in calls[2:]), "重渲仍失败后必须切小"


@pytest.mark.asyncio
async def test_克隆路径不做同文本重渲():
    """克隆路径每次请求都**重采样说话人** ⇒ 重渲会换人 ⇒ 直接切小。"""
    client = _client()
    calls: list[str] = []

    async def _always_short(text, voice=None, output_format="mp3", *, clone=None):
        calls.append(text)
        if len(text) >= 100:
            return _fake_mp3_of_seconds(1)
        return _fake_mp3_of_seconds(60)

    client._synthesize_once = _always_short  # type: ignore[assignment]

    audio = await client._synthesize_chunk("这" * 190, clone=_clone_spec())

    assert audio, "克隆路径切小自愈后应当拼出音频"
    assert len(calls) > 1, "克隆路径必须走自愈"
    assert all(
        len(c) < 190 for c in calls[1:]
    ), "克隆路径**不能**同文本重渲（会换人）⇒ 第一步就必须切小"
