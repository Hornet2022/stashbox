"""音色一致性契约 v1 的可执行断言（2026-10-25）。

这份文件把「契约」里的数字**钉在代码里**，防止它们被悄悄改回去。

锁住的东西：

A. 截断阈值 = 11.0（实测正常上界 9.65，留 14% 余量）
B. 克隆块长 = 800（墙钟 31.8s，对上游 60s 墙钟留 1.9 倍余量）
C. 克隆块长**不被**普通块长夹住（两条路径的墙钟预算不同）
D. crossfade = 100ms（300ms 重叠＝两个独立渲染同时发声 300ms）
E. 判据常量：ECAPA 分离带 / 电平散布 / 段数上限
F. 响度口径是「只修离群 + 整篇归一」，不是「逐段强制等电平」
"""

from __future__ import annotations

import os

import pytest

from stashbox.backend.app.services.tts import audio_chunks, consistency
from stashbox.backend.app.services.tts import openai as openai_mod


# ── A. 截断阈值 ──────────────────────────────────────────────────────────
def test_截断阈值是_11():
    """阈值必须跟着「当前在用的模型」标定过（2026-10-25 实测）。"""
    assert openai_mod._TRUNCATION_CHARS_PER_SEC == 11.0


def test_阈值高于今天实测的正常上界():
    """今天上游的正常语速上界是 9.65（250 字克隆块实测）。

    阈值必须**高于**它，否则正常块会被劈开 → 凭空多出音色接缝
    （生产实测：老阈值 6.8 在 24h 内误判 107 次）。
    """
    observed_normal_max = 9.65
    assert openai_mod._TRUNCATION_CHARS_PER_SEC > observed_normal_max


def test_没有按长度分档的阈值():
    """实测 cps 与**块长无关**（只与文本内容有关）⇒ 不该有两套阈值。

    旧的两个常量（`_MIN_CHARS_FOR_RATE_CHECK` / `_SHORT_TEXT_MAX_CPS`）
    已在 2026-10-25 删除 —— 留着只会在下次有人抄回旧结论。
    """
    assert not hasattr(openai_mod, "_MIN_CHARS_FOR_RATE_CHECK")
    assert not hasattr(openai_mod, "_SHORT_TEXT_MAX_CPS")


# ── B/C. 克隆块长 ────────────────────────────────────────────────────────
def test_克隆块长默认是_800():
    """800 字 → 墙钟 31.8s → 对上游 60s 墙钟留 1.9 倍余量。"""
    assert openai_mod._CLONE_CHUNK_CHARS_DEFAULT == 800


def test_克隆块长的余量足够():
    """墙钟 48ms/字 → 800 字 ≈ 32s；余量不到 1.5 倍就不该取这个值。"""
    ms_per_char = 48.0
    wall = openai_mod._CLONE_CHUNK_CHARS_DEFAULT * ms_per_char / 1000.0
    margin = 60.0 / wall
    assert (
        margin >= 1.5
    ), f"克隆块长 {openai_mod._CLONE_CHUNK_CHARS_DEFAULT} 的墙钟余量只有 {margin:.2f} 倍"


# ── D. crossfade ─────────────────────────────────────────────────────────
def test_crossfade_默认是_100ms():
    """300ms 重叠 = 两个独立渲染同时发声 300ms ⇒ 就是「好几种音色」的听感来源。"""
    assert audio_chunks._CROSSFADE_SEC_DEFAULT == 0.1


def test_crossfade_可由环境变量覆盖(monkeypatch):
    monkeypatch.setenv("OPENAI_TTS_CROSSFADE_SEC", "0.05")
    assert audio_chunks._crossfade_sec() == pytest.approx(0.05)


def test_crossfade_环境变量非法时用默认值(monkeypatch):
    monkeypatch.setenv("OPENAI_TTS_CROSSFADE_SEC", "abc")
    assert audio_chunks._crossfade_sec() == audio_chunks._CROSSFADE_SEC_DEFAULT
    monkeypatch.setenv("OPENAI_TTS_CROSSFADE_SEC", "-1")
    assert audio_chunks._crossfade_sec() == audio_chunks._CROSSFADE_SEC_DEFAULT


# ── E. 判据常量 ──────────────────────────────────────────────────────────
def test_ecapa_分离带留有余量():
    """分离带两侧都要有余量（标定：同一人 ≥0.75，换人 ≤0.35）。"""
    assert consistency.ECAPA_SAME_MIN > consistency.ECAPA_THRESHOLD
    assert consistency.ECAPA_THRESHOLD > consistency.ECAPA_DIFF_MAX
    assert consistency.ECAPA_SAME_MIN - consistency.ECAPA_THRESHOLD >= 0.15
    assert consistency.ECAPA_THRESHOLD - consistency.ECAPA_DIFF_MAX >= 0.15


def test_段数上限():
    """段数 = 接缝数 + 1。块长 800 ⇒ 2000 字的稿子 ≤ 3 段。"""
    assert consistency.MAX_SEGMENTS == 4


def test_响度散布门槛():
    """实测段间散布 0.05 dB ⇒ 1.0 dB 的门槛只作「上游行为变了」的哨兵。"""
    assert consistency.LOUDNESS_SPREAD_MAX_DB == 1.0


# ── F. 响度口径 ──────────────────────────────────────────────────────────
def test_响度归一是整篇口径():
    """`align_and_normalize` 必须走「只修离群 + 整篇归一」。"""
    import inspect

    src = inspect.getsource(audio_chunks.align_and_normalize)
    assert "normalize_article_loudness" in src
    # 老行为（逐段强制等电平）不许再出现
    assert "normalize_loudness(piece)" not in src


def test_死区与增益上限是正数():
    assert audio_chunks.normalize_article_loudness.__defaults__[1] > 0  # dead_zone_db
    assert audio_chunks.normalize_article_loudness.__defaults__[2] > 0  # max_gain_db


# ── 判据模块自身的行为 ───────────────────────────────────────────────────
def test_段数超限判不通过():
    """段数超上限 ⇒ 不通过（接缝是「听出好几种音色」的直接来源）。"""
    pieces = [b"x" * 100 for _ in range(consistency.MAX_SEGMENTS + 1)]
    report = consistency.judge_article(pieces)
    assert not report.passed
    assert any("段数" in p for p in report.problems)


def test_段数正常判通过():
    """段数正常 + 量不到电平 ⇒ 通过（「不可判」不等于「不一致」）。"""
    pieces = [b"x" * 100 for _ in range(2)]
    report = consistency.judge_article(pieces)
    assert report.segments == 2
    assert report.seams == 1


def test_环境变量不会泄漏到测试之间(monkeypatch):
    """_crossfade_sec 读环境变量；测试里设了要能还原。"""
    monkeypatch.delenv("OPENAI_TTS_CROSSFADE_SEC", raising=False)
    assert audio_chunks._crossfade_sec() == audio_chunks._CROSSFADE_SEC_DEFAULT
    assert "OPENAI_TTS_CROSSFADE_SEC" not in os.environ


# ── 上游请求闸门 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_上游请求不并发():
    """闸门：同一进程里**不能**有两个 TTS 请求同时打向上游。

    ## 为什么这是硬要求

    上游**串行处理**（实测：并发 1/2/4 的总耗时 54.2/51.1/53.4s ——
    并发对它零吞吐贡献），而它那条 **60s 墙钟包含它在队列里等的时间**
    （实测：把上游打满后，**80 字**的请求连续 504 —— 80 字推理只要 ~4s）。

    ⇒ 两个请求同时打过去，后一个会在**上游**排到超时；
    ⇒ 在我们这边排队就不计入它的墙钟（我们的 timeout 是 300s）。
    """
    import asyncio

    from stashbox.backend.app.services.tts.openai import OpenAITTSClient

    inflight = 0
    peak = 0

    class _Resp:
        status_code = 200
        content = b"x" * 200
        headers: dict = {}

        def json(self):
            return {}

    async def _post(url, json=None, **kw):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return _Resp()

    client = OpenAITTSClient(api_key="k", base_url="http://x", model="m")
    client._client.post = _post  # type: ignore[method-assign]

    await asyncio.gather(
        client._synthesize_once("甲" * 20),
        client._synthesize_once("乙" * 20),
        client._synthesize_once("丙" * 20),
    )

    assert peak == 1, f"上游同时收到了 {peak} 个请求 —— 闸门没生效"
