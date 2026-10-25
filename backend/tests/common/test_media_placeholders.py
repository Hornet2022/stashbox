"""正文配图占位符的**两种形态**与互转（2026-10-25）。

## 为什么这个契约要单独锁

占位符有两个形态，**跨两个服务**用：

| 形态 | 样子 | 用在哪 |
|---|---|---|
| 内存态 | `\x00IMG3\x00`（真 NUL） | 解析阶段、正文里定位 |
| 落库态 | `\x00IMG3\x00`（可见记法） | PostgreSQL JSONB 里 |

为什么必须分两种：**PostgreSQL 的 JSONB 不接受 `\u0000`**
（实测报 `unsupported Unicode escape sequence`）⇒ 带 NUL 的记录落库会炸。

为什么必须**可逆**：识图挪出剪藏请求后（2026-10-25），文章是**带着占位符
先落库**、再由蒸馏任务补图注 ⇒ 补图注时要把落库态**还原成内存态**
才能定位到正文里的位置。

⇒ 这份契约被 content-service 与 ai-service 同时依赖 ⇒ 放在 `common/` 下
**只有一份实现**。两边各写一份正则，就会只改到一半（历史上就是这么漏的）。

## 这几个用例锁的是"不会静默退化"

最阴的退化不是报错，是**占位符认不回来**：
`_strip_nul` 把 NUL 删掉 ⇒ `\x00IMG3\x00` 变成裸的 `IMG3` ⇒
补图注时匹配不到位置 ⇒ 图表信息**静默消失**，正文看着还挺正常。
所以往返（roundtrip）必须逐位相等。
"""

from __future__ import annotations

from stashbox.backend.common import media_placeholders as placeholders


def test_内存态占位符用_nul_哨兵():
    """正常文字里不会出现 NUL ⇒ 不会和真实内容碰撞。"""
    token = placeholders.make_token(3)
    assert token == "\x00IMG3\x00"
    assert "\x00" in token


def test_落库态是可见记法():
    """JSONB 不接受 `\u0000` ⇒ 落库形态必须是**看得见**的字面量。"""
    stored = placeholders.to_storage_token(placeholders.make_token(3))
    assert stored == "\\x00IMG3\\x00"
    assert "\x00" not in stored
    # 落库形态里不该再出现真 NUL
    assert not any(ord(ch) == 0 for ch in stored)


def test_往返逐位相等():
    """**这是最关键的一条**：落库态 → 内存态 必须能还原回去。

    还原不了 ⇒ 补图注时匹配不到正文里的位置 ⇒ 图表信息静默消失
    （正文看着还挺正常，所以这个 bug 极难被发现）。
    """
    original = "第一段。\n\n" + placeholders.make_token(7) + "\n\n第二段。"
    stored = placeholders.to_storage_text(original)
    assert "\x00" not in stored, "落库形态不能带 NUL"
    assert placeholders.from_storage_text(stored) == original


def test_正文转换只动占位符_其它文字不动():
    text = "价格是 100 元，不是 0x00IMG1x00 这种。"
    stored = placeholders.to_storage_text(text)
    assert stored == text, "不是占位符的内容一个字节都不该动"


def test_剥占位符_两种形态都剥():
    """兜底：绝不让占位符进 LLM 的 prompt。"""
    text = (
        "第一段。"
        + placeholders.make_token(1)
        + "中间"
        + placeholders.to_storage_token(placeholders.make_token(2))
        + "结尾"
    )
    stripped = placeholders.strip_placeholders(text)
    assert "\x00" not in stripped
    assert "IMG" not in stripped
    assert "第一段。" in stripped and "中间" in stripped and "结尾" in stripped


def test_序号提取():
    assert placeholders.token_index(placeholders.make_token(12)) == 12
    assert placeholders.token_index("IMG3") is None
    assert placeholders.token_index("") is None


def test_空文本不炸():
    assert placeholders.strip_placeholders("") == ""
    assert placeholders.to_storage_text("") == ""
    assert placeholders.from_storage_text("") == ""
