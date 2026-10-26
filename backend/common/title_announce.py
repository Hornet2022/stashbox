"""标题播报（2026-10-26）：音频在播正文之前，先说明本期标题。

## 为什么需要

内测反馈：「音频在播放内容前，能不能先说明一下内容的标题，现在直接开始读正文，
播放列表的内容切换很突兀」。

问题不在音频本身，在于**听的人不知道自己在听什么**：从播放列表切到下一篇时，
上一篇的尾音直接接上下一篇的开场，中间没有任何「这是一期新的内容」的信号。
把标题播出来，听众立刻知道「换台了」。

## 口径（唯一实现，所有产出音频的路径共用这一个函数）

- 播报句：`本期分享的标题是：{标题}。`
- 标题为空 / 清洗后为空 → **不播报**（保持旧行为，不塞空话）
- 开关：环境变量 ``TTS_TITLE_ANNOUNCE``（默认 **开**；设 ``0/false/off/no/none`` 关）

⚠️ 为什么必须共用一个函数、而不是在各个流水线里各写一句：
本仓有**两条**产出音频的路径（agent 活路径 `agent/runner.py` 与旧
`distill/pipeline.py`），外加**两条**消费稿件首段的路径（few-shot 入池
`evaluation_service`、App 详情页导语）。各写一套 ⇒ 口径漂移，且漂移是**静默**的
（少播一次标题没人会发现；但把播报句当成 hook 塞进 few-shot 池，会让所有后续
改写都学会用同一句开场 —— 这正是本文件存在的理由）。

## 播报句为什么进 ``script_text``（而不是只进音频）

App 的字幕时间轴（``SubtitleTimeline``）是**从 script_text 按字数比例**估出来的
（生产的 TTS provider 不返回词级时间戳）。如果音频开头多播了 4 秒而
script_text 里没有这 4 秒对应的文字，字幕就会**整体早 4 秒**，误差随播放进度
线性放大 —— 听到结尾时字幕已经跑在音频前面好几秒。

所以播报句必须同时进音频与 script_text（音画同源），代价是「script_text 的第一段
不再等于 hook」这个旧契约要显式改掉：

- 真正的 hook 由服务端**明确**放进 ``distilled_articles.content_parts.hook``；
- few-shot 入池改读 ``content_parts.hook``（没有则回退旧的取法，兼容存量数据）；
- App 详情页的导语同样读 ``content_parts.hook``，播报句本身不作为正文展示。

## 标题清洗为什么需要这么多条规则

清洗不是为了"好看"，每条都对应一个**具体的失败**：

1. 换行 / 制表符 → 空格：否则 TTS 的切块（``split_into_chunks`` 按句末标点切）
   会把标题切两半，或让 TTS 念出换行。
2. 去掉书名号 / 引号类符号：``《》「」""''`` 这类符号各家 TTS 处理不一致，
   常见结果是**吞字**（整段标题读不出来）或把符号念出来。
3. 标题**内部**的句末标点（``。！？；``）→ 空格：标题里的句号会让切块把标题
   从中间劈开，听感上是「念到一半断了」。
4. ``_`` → 空格：文件名型标题（``reference-vs-rag_深度技术调研报告_v1.0``）
   的下划线会被念成"下划线"或直接吞掉。
5. 压缩连续空白（含全角空格）：避免播报句中间出现一串气口。
6. 长度上限：标题超过 40 字就播报前 40 字 + 省略号 —— 播报是导览不是朗读全文，
   太长的标题会让人等不及正文。
"""

from __future__ import annotations

import os
import re

__all__ = [
    "ANNOUNCE_TEMPLATE",
    "MAX_TITLE_CHARS",
    "build_title_announcement",
    "is_enabled",
    "prepend_title_announcement",
    "sanitize_title_for_speech",
]

# 播报句模板。改这里的措辞 = 改全站的开场口径。
ANNOUNCE_TEMPLATE = "本期分享的标题是：{title}。"

# 标题播报的最大字数（清洗后）。超出部分截断 + 省略号。
MAX_TITLE_CHARS = 40

# 关掉播报的开关取值（小写比较）。
_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "none", "disabled"})

# 书名号 / 引号类符号：TTS 处理不一致，常见结果是吞字。
_BRACKET_CHARS = "《》〈〉「」『』【】“”‘’\"'"

# 标题内部的句末标点：会把标题从中间切开。
_SENTENCE_PUNCT = "。！？!?；;…"

# 连续空白（含全角空格 / 制表符 / 换行）。
_WS_RE = re.compile(r"[\s　]+")


def is_enabled() -> bool:
    """播报开关。**每次调用时读 env**（不在 import 时固化）。

    为什么要动态读：这是线上可回退的开关，admin 改 env 重启服务就生效；
    固化成 import 时的常量会让"改了 env 却没生效"变成一个查不出来的玄学问题。
    """
    raw = os.getenv("TTS_TITLE_ANNOUNCE")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def sanitize_title_for_speech(title: str | None) -> str:
    """把标题清洗成"TTS 能顺畅念出来"的一行文本。

    规则见模块 docstring。返回值可能是空串（标题清洗后没剩什么）。
    """
    if not title:
        return ""
    text = str(title)
    # 1. 换行 / 制表符 → 空格（保留分段感，但不制造断句）
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    # 2. 去掉书名号 / 引号类符号
    text = "".join(ch for ch in text if ch not in _BRACKET_CHARS)
    # 3. 标题内部的句末标点 → 空格（否则切块会把标题劈开）
    text = "".join(" " if ch in _SENTENCE_PUNCT else ch for ch in text)
    # 4. 下划线 → 空格（文件名型标题）
    text = text.replace("_", " ")
    # 5. 压缩连续空白
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    # 6. 长度上限
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip() + "…"
    return text


def build_title_announcement(title: str | None) -> str | None:
    """生成播报句；不该播的时候返回 ``None``。

    返回 ``None``（而不是空串）是有意的：调用方据此判断"要不要插这一段"，
    空串会被误当成"插了一段空的"。
    """
    if not is_enabled():
        return None
    clean = sanitize_title_for_speech(title)
    if not clean:
        return None
    return ANNOUNCE_TEMPLATE.format(title=clean)


def prepend_title_announcement(script: str, title: str | None) -> tuple[str, str | None]:
    """把播报段插到稿件最前面。

    Returns:
        ``(新稿, 播报句)``。没有播报句时原样返回 ``(script, None)``。

    ⚠️ 段落边界用 ``\\n\\n``：这是全仓"一段 = 一段"的既有约定
    （script_text / 字幕分段 / few-shot 取首段都按它切）。用单换行会被
    ``split("\\n\\n")`` 忽略，播报句就会和 hook 粘成一段。
    """
    announcement = build_title_announcement(title)
    if not announcement:
        return script, None
    body = (script or "").strip()
    if not body:
        return announcement, announcement
    return f"{announcement}\n\n{body}", announcement
