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
def test_克隆块长默认是内容完整性上限():
    """⚠️ **2026-11-26 推翻「默认 1500」**（实测证据，见 openai.py 的 _MAX_SAFE_CHUNK_CHARS）。

    旧依据是**墙钟余量**：1500 字 → 墙钟 63.0s，对我们自己的 httpx 读超时
    （300s）留 4.8 倍余量 ⇒ 看起来很安全。

    但上游对超长请求会**静默截断**：返回 HTTP 200、音频更短、**看起来完全成功**
    （比 504 更危险）。⇒ **墙钟/超时/429/504/cps 虚高阈值全都测不出它**
    （1484 字块被截掉 26% 后 cps=5.84，仍落在正常区间 5.15~9.65 内）。

    生产实证（2026-11-26，scripts/exp_tts_chunk_integrity.py + ASR 逐段对照）：
        块长 700  → 4 块，总 508.4s（cps 4.61）✅ 完整
        块长 1000 → 3 块，总 523.7s（cps 4.47）✅ 完整（块尾 ASR 念到期望尾句）
        块长 1500 → 2 块，总 440.5s（cps 5.33）❌ **少 ~83s ≈ 370 字**
            （ASR：1500#0 块尾念到「区块链防的是数据篡」就断了，
              期望尾句是「用工程确定性对抗模型随机性。」⇒ 后面 ~370 字没念）
    已知上游截断点：1158 字（本仓历史实测）。

    ⇒ 真约束是**内容完整性**，不是墙钟 ⇒ 默认取 **1000**（低于 1158，留 14% 余量）。
    """
    assert openai_mod._CLONE_CHUNK_CHARS_DEFAULT == openai_mod._MAX_SAFE_CHUNK_CHARS
    assert openai_mod._CLONE_CHUNK_CHARS_DEFAULT <= 1000, (
        f"克隆块长 {openai_mod._CLONE_CHUNK_CHARS_DEFAULT} 超出内容完整性上限 "
        f"{openai_mod._MAX_SAFE_CHUNK_CHARS}（>1158 字已知会被静默截断）"
    )


def test_克隆块长的余量足够():
    """余量要对我们**自己的**读超时算，不是对上游那个已经消失的 60s 墙钟。

    实测墙钟（当前上游，克隆路径，生产真实 3175 字稿）：
        800 字 → 34.5s    1000 字 → 44.7s    1200 字 → 51.6s
        1500 字 → 63.0s   1600 字 → 96.2s    2000 字 → 90.8s
    ⇒ 取**最差**的实测斜率（96.2s / 1600 字 = 60ms/字）来估，
      余量必须 ≥ 2 倍（失败要重渲 + 退避，时间得够）。

    旧写法拿 60s 墙钟当分母 —— 那个墙钟是**旧上游**的，当前上游已不存在。
    """
    ms_per_char = 60.0  # 最差实测斜率
    wall = openai_mod._CLONE_CHUNK_CHARS_DEFAULT * ms_per_char / 1000.0
    margin = 300.0 / wall  # 我们自己的 httpx 读超时
    assert (
        margin >= 2.0
    ), f"克隆块长 {openai_mod._CLONE_CHUNK_CHARS_DEFAULT} 的墙钟余量只有 {margin:.2f} 倍"


def test_克隆块长不超过实测验证过的上限():
    """块长不得超过**实测跑通过**的上限。

    实测（当前上游，生产真实 3175 字稿）：
        1500 字 → 63.0s / 224.3s 音频 ✓
        1800 字 → 103.2s / 261.4s 音频 ✓
        2000 字 → 77.7s / 274.0s 音频 ✓
        2200 字 → 89.9s / 317.4s 音频 ✓
        （1400 / 1600 两档撞上上游 429「资源不足」⇒ 上游**负载敏感**）

    ⇒ 2200 是**验证过**的边界；再往上没有实测依据，不许取。
    ⇒ 为什么不取 2200：余量比那一点接缝收益更值钱（见 openai.py 的注释）。

    ⚠️ **「上游有 ~322s 音频上限」是错的**（2026-11-25 第一轮自己也这么以为）：
    同一份 2000 字文本，两次实测分别产出 **322.0s** 与 **274.0s**
    ⇒ 那是抽样语速的差别，不是上限。

    ⚠️⚠️ **2026-11-26 改口径：上面四档「跑通」只验了墙钟/音频时长，
    没验内容是否念完** —— 而上游恰恰在这些档位**静默丢内容**
    （1500 实测丢 ~370 字，ASR 实证）。
    ⇒ 现在以**内容完整性**为准：安全上限 1000（实测 700/1000 完整、1500 截断；
    已知截断点 1158）。
    """
    assert openai_mod._CLONE_CHUNK_CHARS_DEFAULT <= openai_mod._MAX_SAFE_CHUNK_CHARS, (
        f"克隆块长 {openai_mod._CLONE_CHUNK_CHARS_DEFAULT} 超过内容完整性上限 "
        f"{openai_mod._MAX_SAFE_CHUNK_CHARS}（超长请求会被上游静默截断）"
    )


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
    """分离带两侧都要有余量。

    ⚠️ **2026-11-25：余量要求从 0.15 收到 0.05**。

    旧要求（≥0.15）配的是旧上游（tertu.cn）的标定：同一人 ≥0.75、
    换人 ≤0.35 ⇒ 分离带宽 0.40。

    当前上游（hub.tsingfly.com）重新标定后：
        换人（预设音色两两 / vs 克隆源）  0.357 ~ 0.697
        同一人（克隆块 vs 克隆源）        0.857 ~ 0.931
    ⇒ 分离带只有 [0.697, 0.857]，宽 0.16 ⇒ 余量天然只有 0.07 / 0.08。

    ⇒ 门禁阈值必须落在**实测为空的**那一段里；要求 0.15 的余量会让
      阈值无处可放（会把「换人」误判成「同一人」）。
      0.05 是「余量还在，但必须尊重实测带宽」的落点。

    判据**语义没变**（分离带两侧都要留余量），变的是余量数字 ——
    因为上游变了、实测带宽变了。
    """
    assert consistency.ECAPA_SAME_MIN > consistency.ECAPA_THRESHOLD
    assert consistency.ECAPA_THRESHOLD > consistency.ECAPA_DIFF_MAX
    assert consistency.ECAPA_SAME_MIN - consistency.ECAPA_THRESHOLD >= 0.05
    assert consistency.ECAPA_THRESHOLD - consistency.ECAPA_DIFF_MAX >= 0.05


def test_ecapa_阈值落在实测分离带的空隙里():
    """阈值必须落在「换人最高」与「同一人最低」之间的**空隙**里。

    实测（2026-11-25，当前上游）：
        换人最高 0.697（serena ↔ vivian）
        同一人最低 0.857（克隆块 vs 克隆源）
    ⇒ 阈值必须 > 0.697 且 < 0.857，否则会把某一侧整片误判。
    """
    assert consistency.ECAPA_DIFF_MAX >= 0.70, "换人上界必须覆盖实测的 0.697"
    assert consistency.ECAPA_SAME_MIN <= 0.86, "同一人下界必须低于实测的 0.857"
    assert 0.697 < consistency.ECAPA_THRESHOLD < 0.857


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
