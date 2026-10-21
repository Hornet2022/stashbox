"""长文分段改写：把「一次长输入」拆成「多次短输入」。

## 为什么

上游 vLLM 会**随机**把生成内容整个丢掉（HTTP 200、content 为空、
completion_tokens > 0）。实测（2026-10-21，tertu.cn 的 Qwen3.8-27B）：

    user message  735 字 → 50% 空
    user message 1465 字 → 100% 空
    user message 2925 字 → 100% 空

**空率随 prompt 长度急剧上升** —— 这才是「长文必失败」的根因，
不是超时、不是缓存、不是退避。库里 2920 字那篇文章连续 4 次尝试全空，
而 800 字那篇一次就过。

## 为什么不是「让 agent 自己去取原文」

agent 确实有 `fetch_url` tool（见 tools.py）、有 MCP 客户端、有 few-shot 记忆池。
但**原文最终必须进 LLM 上下文才能被改写** —— agent 自己取回原文后照样要塞进
prompt，长度约束原封不动，空 content 的风险也还在。

除非「取回来」的不是全文而是**要点**，那本质上就是两阶段
（抽取 → 写稿）：多一轮 LLM 调用，且中间那层「要点」是有损的。

所以这里选**分段改写**：每块单独改写，直接从原文到稿子，
不经过有损的中间层。

## 分段策略

- 按**段落边界**（`\\n\\n`）贪心装箱，每块 ≤ `REWRITE_CHUNK_CHARS`；
  单段超长时按句读边界再切（复用 audio_chunks.split_into_chunks 的思路）。
- 每块独立调用 LLM 改写 → 各自产出 {hook, sections, outro}。
- 合并规则（保住播客稿的结构）：
  - hook   = **第一块**的 hook（只有开头需要钩子）
  - outro  = **最后一块**的 outro（只有结尾需要收束）
  - sections = 各块的 sections **依次**拼接
  - 中间块的 hook/outro 丢弃（否则会出现多个钩子，稿子结构乱）

## 目标字数

原来 system prompt 里写死了「原文 < 500 字 → 600-800 字；500-2000 字 →
800-1100 字；> 2000 字 → 1000-1400 字」。分段后每块都按这个规则会**严重超长**
（5 块 × 800 字 = 4000 字）。所以每块要按**总目标字数 / 块数**来分配。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: 单块原文的最大字数（user message 口径，不含 system）。
#:
#: 实测（2026-10-21）：735 字 → 50% 空，1465 字 → 100% 空。
#: 取 500 是往安全侧留 —— 配合 LLM 层 4 次尝试 + Arq 层 5 次任务重试，
#: 整体失败率降到可忽略。
REWRITE_CHUNK_CHARS = 500

#: 整稿的目标字数区间（按原文长度自适应）。
#:
#: 与原 system prompt 里的规则同源，只是搬到这里显式传给每块，
#: 避免分段后每块都按「全文长度」去写，导致总稿超长。
_TARGET_BY_SOURCE_LEN: list[tuple[int, int]] = [
    (500, (600, 800)),
    (2000, (800, 1100)),
    (10**9, (1000, 1400)),
]

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")


def target_chars_for(source_len: int) -> tuple[int, int]:
    """按原文长度算整稿目标字数区间。"""
    for limit, target in _TARGET_BY_SOURCE_LEN:
        if source_len < limit:
            return target
    return _TARGET_BY_SOURCE_LEN[-1][1]


def split_for_rewrite(content: str, limit: int = REWRITE_CHUNK_CHARS) -> list[str]:
    """把原文切成若干块，每块 ≤ limit 字（尽量在段落边界切）。

    - 先按段落（空行）分句，贪心装箱；
    - 单个段落超长时，按句读边界切；
    - 都不行时按硬切兜底（不丢字）。
    """
    if limit <= 0 or len(content) <= limit:
        return [content]

    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paragraphs:
        if len(para) > limit:
            if cur.strip():
                chunks.append(cur.strip())
                cur = ""
            chunks.extend(_split_long_paragraph(para, limit))
            continue
        if len(cur) + len(para) + 2 <= limit:
            cur = f"{cur}\n\n{para}" if cur else para
        else:
            if cur.strip():
                chunks.append(cur.strip())
            cur = para
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _split_long_paragraph(paragraph: str, limit: int) -> list[str]:
    """超长段落：按句读边界切，必要时硬切。"""
    sentences = [s for s in _SENT_SPLIT_RE.split(paragraph) if s.strip()]
    pieces: list[str] = []
    cur = ""
    for sent in sentences:
        if len(sent) > limit:
            if cur.strip():
                pieces.append(cur.strip())
                cur = ""
            rest = sent
            while len(rest) > limit:
                pieces.append(rest[:limit])
                rest = rest[limit:]
            cur = rest
            continue
        if len(cur) + len(sent) <= limit:
            cur += sent
        else:
            if cur.strip():
                pieces.append(cur.strip())
            cur = sent
    if cur.strip():
        pieces.append(cur.strip())
    return pieces
