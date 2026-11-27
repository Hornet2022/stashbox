"""OpenAI TTS **块长内容完整性**守卫（2026-11-26）。

## 为什么要有这条

2026-11-25 把生产 `OPENAI_TTS_CHUNK_CHARS` 从 700 上调到 1500，
2026-11-26 实测发现：**上游对超长请求会静默截断**
（返回 HTTP 200、音频更短、看起来完全成功），
新蒸馏音频**整块丢掉 397 字**（related[2]+related[3]+insights[0]），
App 上「听感整理稿」有、音频里没有 ⇒ 用户报「音频和稿件内容不一致」。

## 为什么现有防线接不住

* `_TRUNCATION_CHARS_PER_SEC = 11.0`（语速虚高判据）：截断 26% 后 cps=5.84，
  仍落在正常区间 5.15~9.65 ⇒ **不触发**。
* 墙钟 / 429 / 504 重试：截断反而**更快完成**，一律正常。
* ⇒ 静默截断的**唯一有效防线是块长上限**（预防，不是检测）。

## 这条守卫钉住什么

1. 显式传参 / env 都**不能**把块长顶到 `_MAX_SAFE_CHUNK_CHARS` 之上
   （IndexTTS 路径早有同款上界 400 的守卫，本路径此前缺）。
2. 默认值本身安全（≤ 上限）。
3. 上限常量不高于已知的上游截断点（1158 字，本仓历史实测）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts.openai import (  # noqa: E402
    _CLONE_CHUNK_CHARS_DEFAULT,
    _MAX_SAFE_CHUNK_CHARS,
    OpenAITTSClient,
)


def _client(**kw) -> OpenAITTSClient:
    """构造一个不发请求的 client（api_key 假值即可，构造不校验网络）。"""
    kw.setdefault("api_key", "sk-test")
    kw.setdefault("base_url", "http://127.0.0.1:9/v1")
    kw.setdefault("model", "tts-test")
    return OpenAITTSClient(**kw)


def test_max_safe_chunk_chars_below_known_truncation_point():
    """上限必须低于上游已知截断点（1158 字，仓库内历史实测记录）。

    1158 字实测：返回 200 但只产出 124s 音频（应有 276s）⇒ 真截断。
    """
    assert (
        _MAX_SAFE_CHUNK_CHARS <= 1000
    ), f"内容完整性上限 {_MAX_SAFE_CHUNK_CHARS} 已达到/超过已知截断点 1158 字"


def test_default_chunk_chars_are_safe():
    """两条路径的默认块长都必须在安全上限之内。

    ⚠️ 默认值超限 ⇒ 生产只要没设 env 就会踩雷（env 越界有 clamp 兜底，
    但默认值本身不该带雷）。
    """
    c = _client()
    assert (
        0 < c.chunk_chars <= _MAX_SAFE_CHUNK_CHARS
    ), f"默认普通块长 {c.chunk_chars} 超出安全上限 {_MAX_SAFE_CHUNK_CHARS}"
    assert (
        0 < c.clone_chunk_chars <= _MAX_SAFE_CHUNK_CHARS
    ), f"默认克隆块长 {c.clone_chunk_chars} 超出安全上限 {_MAX_SAFE_CHUNK_CHARS}"
    assert _CLONE_CHUNK_CHARS_DEFAULT <= _MAX_SAFE_CHUNK_CHARS


def test_explicit_chunk_chars_are_clamped():
    """显式传参顶到上限之上 ⇒ 被 clamp（而不是照单全收）。"""
    c = _client(chunk_chars=1500, clone_chunk_chars=9999)
    assert c.chunk_chars == _MAX_SAFE_CHUNK_CHARS
    assert c.clone_chunk_chars == _MAX_SAFE_CHUNK_CHARS


def test_env_chunk_chars_cannot_exceed_safety_cap(monkeypatch):
    """env 是「上限以下可调」，不是「随便调」。

    这正是 2026-11-25 的事故入口：`OPENAI_TTS_CHUNK_CHARS=1500`
    ⇒ 静默丢内容 ⇒ 用户听到的音频和稿件对不上。
    """
    monkeypatch.setenv("OPENAI_TTS_CHUNK_CHARS", "1500")
    monkeypatch.setenv("OPENAI_TTS_CLONE_CHUNK_CHARS", "1500")
    c = _client()
    assert c.chunk_chars == _MAX_SAFE_CHUNK_CHARS
    assert c.clone_chunk_chars == _MAX_SAFE_CHUNK_CHARS


def test_env_within_cap_is_respected(monkeypatch):
    """上限以内的 env 照常生效（别把配置能力一起掐死）。"""
    monkeypatch.setenv("OPENAI_TTS_CHUNK_CHARS", "800")
    c = _client()
    assert c.chunk_chars == 800


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_chunk_chars_falls_back(monkeypatch, bad):
    """非正块长 ⇒ 回退到安全默认（0 会被当成「整篇一次」，必然截断）。"""
    monkeypatch.setenv("OPENAI_TTS_CHUNK_CHARS", str(bad))
    c = _client()
    assert c.chunk_chars > 0
    assert c.chunk_chars <= _MAX_SAFE_CHUNK_CHARS
