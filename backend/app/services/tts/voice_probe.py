"""TTS 音色探测：从第三方服务的报错里解析出「支持哪些 voice」。

## 为什么需要（2026-10-21）

运营在后台配 TTS 时最常踩的坑就是 **voice 名不对**：
不同服务的音色命名完全不一样 ——

- OpenAI 官方：``alloy`` / ``echo`` / ``fable`` / ``onyx`` / ``nova`` / ``shimmer``
- 火山方舟：``BV001_streaming`` / ``BV002_streaming`` …
- 第三方 OpenAI 兼容服务（实测到的）：``aiden, dylan, eric, ono_anna, ryan,
  serena, sohee, uncle_fu, vivian``

代码里 ``OpenAITTSClient`` 的默认 voice 是 ``alloy``。用户接一个**非 OpenAI
官方**的兼容服务时，服务会返回 400：

    Invalid voice 'alloy'. Supported: aiden, dylan, eric, ono_anna, ryan,
    serena, sohee, uncle_fu, vivian

这句话里**就有答案**，但它埋在 error 的 detail 里，前端只显示
"服务端处理异常" —— 运营只能去翻日志，或者一个一个试。

## 做法

不发"探测请求"，而是**从已有的错误信息里正则提取**：

1. ``Invalid voice 'xxx'. Supported: a, b, c`` —— 多数兼容服务的格式
2. ``Supported voices: a, b, c`` —— 另一种写法
3. ``"voice" must be one of ["a", "b"]`` —— JSON 风格

提取不到就返回空列表（不猜、不硬编码）。
"""

from __future__ import annotations

import re

# 形式 1：Invalid voice 'x'. Supported: a, b, c
#         也覆盖 "Supported voices: a, b" / "supported: a, b"
_SUPPORTED_AFTER_COLON = re.compile(
    r"supported(?:\s+voice)?s?\s*[:：]\s*([A-Za-z0-9_,\-\s]+)",
    re.IGNORECASE,
)

# 形式 3：must be one of ["a", "b"] / must be one of ('a', 'b')
_MUST_BE_ONE_OF = re.compile(
    r"must be one of\s*[\[\(]([^\]\)]+)[\]\)]",
    re.IGNORECASE,
)

# 形式 2：voice 名列表（从引号里抠）
_QUOTED = re.compile(r"['\"]([A-Za-z0-9_\-]+)['\"]")

# 兜底：把分隔符统一成逗号前的空格
_SPLIT = re.compile(r"[,\s]+")


def _clean(raw: str) -> list[str]:
    """把一段原始列表文本切成干净的音色名列表。"""
    out: list[str] = []
    for part in _SPLIT.split(raw):
        p = part.strip().strip("'\"").strip()
        if not p:
            continue
        # 过滤掉句子尾巴的标点残留
        p = p.rstrip(".,;:。；，")
        if not p:
            continue
        if p.lower() in {
            "and",
            "or",
            "the",
            "voice",
            "voices",
            "supported",
            "one",
            "of",
            "is",
            "not",
            "null",
            "none",
            "true",
            "false",
        }:
            continue
        out.append(p)
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def extract_supported_voices(text: str | None) -> list[str]:
    """从任意错误文本里提取支持的 voice 列表。

    提取不到返回 ``[]``（**不猜、不硬编码**）。

    >>> extract_supported_voices("Invalid voice 'alloy'. Supported: aiden, ryan")
    ['aiden', 'ryan']
    >>> extract_supported_voices("no voices here") == []
    True
    """
    if not text:
        return []

    # 形式 3 先匹配（JSON 风格的内容更明确）
    m = _MUST_BE_ONE_OF.search(text)
    if m:
        quoted = _QUOTED.findall(m.group(1))
        if quoted:
            return _clean(" ".join(quoted))

    # 形式 1 / 2
    m = _SUPPORTED_AFTER_COLON.search(text)
    if m:
        voices = _clean(m.group(1))
        if voices:
            return voices

    return []
