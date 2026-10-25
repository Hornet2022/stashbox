"""正文配图占位符的**两种形态**与互转（content-service 与 ai-service 共用一份）。

## 为什么有两种形态

占位符在**内存**里用 NUL 哨兵（``\\x00IMG3\\x00``）：正常文字里不会出现 NUL，
所以它不会和真实内容碰撞。

但 **PostgreSQL 的 JSONB 不接受 ``\\u0000``**（实测报
``unsupported Unicode escape sequence``）⇒ 落库前必须换成**可见记法**
（字面量 ``\\x00IMG3\\x00``，即反斜杠 + x + 0 + 0 四个可见字符）。

## 为什么现在要求「落库态可逆」

2026-10-25 之前，识图是在**剪藏请求里**同步做的 ⇒ 落库时占位符已经被图注
替换掉了，落库形态不需要可逆。

改成「识图挪到蒸馏任务里」之后（剪藏请求不再等识图），文章会**先带着占位符
落库**、再由蒸馏任务补图注 ⇒ 占位符的落库形态必须**能还原回内存形态**，
否则补图注时找不到位置（`apply_captions` 匹配的是内存态的 NUL 哨兵）。

⇒ 这份契约被两个服务用 ⇒ 放在 ``common/`` 下**只有一份实现**，
   不要在两边各写一份正则（写两份就会只改到一半）。
"""

import re

#: 内存态占位符：NUL 哨兵 + 序号 + NUL 哨兵。
TOKEN_RE = re.compile("\x00IMG(\\d+)\x00")

#: 落库态占位符：可见记法（JSONB 能存）。
STORAGE_TOKEN_RE = re.compile(r"\\x00IMG(\d+)\\x00")


def make_token(index: int) -> str:
    """第 index 张图的占位符（index 从 1 开始）—— **内存态**。"""
    return f"\x00IMG{index}\x00"


def token_index(token: str) -> int | None:
    """占位符 → 序号；不是占位符返回 None。"""
    match = TOKEN_RE.fullmatch(token)
    return int(match.group(1)) if match else None


def to_storage_token(token: str) -> str:
    """**内存态** token → **落库态** token。

    为什么必须有这一步：占位符里带 NUL 哨兵（正常文字里不会出现，
    所以不会和真实内容碰撞），而 **PostgreSQL 的 JSONB 不接受 ``\\u0000``**
    （实测报 ``unsupported Unicode escape sequence``）——
    带 NUL 的记录一旦落库，整条建库就炸在那里。

    ⇒ 内存里用 NUL 哨兵，落库前换成同名可见记法（可逆、信息不丢）。
    """
    return token.replace("\x00", "\\x00")


def to_storage_text(text: str) -> str:
    """正文里的占位符 → **落库态**（其余 NUL 不管，交给别处剥）。

    为什么正文要单独处理：`_strip_nul`（FetchResult → JSONB）会把所有 NUL
    直接删掉 ⇒ 占位符会退化成裸的 ``IMG3``，**再也认不回来**。
    ⇒ 正文里的占位符必须先换成可见记法，再走剥 NUL。
    """
    return TOKEN_RE.sub(lambda m: to_storage_token(m.group(0)), text)


def from_storage_text(text: str) -> str:
    """正文里的占位符 → **内存态**（落库态 → 内存态）。

    用于「文章先带着占位符落库、之后再补图注」这条链：补图注时要拿
    内存态的 token 去匹配正文里的位置。
    """
    return STORAGE_TOKEN_RE.sub(lambda m: make_token(int(m.group(1))), text)


def strip_placeholders(text: str) -> str:
    """把正文里所有占位符删掉（**两种形态**都删）。

    兜底：绝不让占位符进 LLM。识图失败、端点没被调用、或者任何异常路径，
    正文里都不该剩下 ``\\x00IMG3\\x00`` 这种东西。
    """
    if not text:
        return text
    out = STORAGE_TOKEN_RE.sub("", text)
    return TOKEN_RE.sub("", out)


__all__ = [
    "STORAGE_TOKEN_RE",
    "TOKEN_RE",
    "from_storage_text",
    "make_token",
    "strip_placeholders",
    "to_storage_text",
    "to_storage_token",
    "token_index",
]
