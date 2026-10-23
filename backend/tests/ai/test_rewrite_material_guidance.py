"""改写 prompt 的**素材类型分支**（2026-10-23）。

## 为什么要有这个测试

文档剪藏之后，改写 prompt 若还用「公众号文章」那套口径，产出的稿子会**逼出编造**：

- 「hook 要狠…可以是反问、矛盾」→ 文档没有悬念 → 硬造 = 编造；
- 「不要复读原文，要重述 + 加你的解读」→ README/文档的价值恰恰是**准确**；
- 「>2000 字 → 1000-1400 字」→ 文档常上万字 → 与「不要漏点」**直接冲突**；
- 「不要念代码块」这条根本不存在 → `pip install x` 会被念成「派 pip install」。

所以按素材类型追加一段**差异口径**。这里锁住三件事：

1. 文档走文档口径（追加段非空）；
2. 其它素材（微信/网页）**追加段为空** —— 现有行为一个字都不能变；
3. 差异段里必须包含那几条「不这样会怎样」的关键约束
   （不念代码块 / insights 可为空 / 长度放宽 / 标签不许自造）——
   它们是「文档稿不出胡话」的全部保障，漏一条就退化回公众号口径。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_SERVICE_DIR = BACKEND_DIR / "ai-service"
if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))

import pytest  # noqa: E402

from agent import runner as runner_mod  # noqa: E402


def test_文档走文档口径():
    g = runner_mod._material_guidance("document")
    assert g, "文档必须有专门的口径段"


def test_其它素材保持原样():
    """公众号文章 / 网页 / 抖音：**追加段必须为空**。

    空 = 现有行为一个字都不变 —— 这是「加分支不破坏存量」的基本保证。
    """
    for src in ("wechat_mp", "generic_url", "douyin", "", "unknown"):
        assert runner_mod._material_guidance(src) == "", f"{src} 不该有分支"


def test_pdf_按文档口径():
    """PDF 直链本质就是文档（章节化、无叙事线）→ 走文档口径。"""
    assert runner_mod._material_guidance("pdf")


def test_文档口径包含关键约束():
    """这几条是「文档稿不出胡话」的全部保障，漏一条就退化成公众号口径。"""
    g = runner_mod._material_guidance("document")
    # 不要念代码块/命令/路径
    assert "代码块" in g and "命令" in g
    # insights 在文档上常常不成立 → 允许空数组，不许凑数
    assert "insights" in g
    # 长度自适应要放宽（否则与「不要漏点」冲突）
    assert "长度自适应" in g
    # 标签不许自造（造了订阅推送永远匹配不上）
    assert "候选标签" in g
    # 复述要准确（文档的价值是准确，不是文采）
    assert "准确" in g


def test_默认system_prompt不含素材段():
    """分支是**追加**的，不是把整段塞进主 prompt —— 共同要求只有一份。"""
    assert "素材类型" not in runner_mod._REWRITE_SYSTEM
    assert "素材类型" in runner_mod._material_guidance("document")


def test_改写调用显式给了_max_tokens():
    """⚠️ 不给 max_tokens 就吃默认 4096 → 文档稿的 JSON **被截断** →
    解析失败 → 降级成纯文本 → 稿子的结构全丢（hook/sections/三类全糊成一坨）。

    这个失败是**静默**的：稿子照样生成、照样能播，只是质量塌了。
    """
    src = (BACKEND_DIR / "ai-service" / "agent" / "runner.py").read_text(encoding="utf-8")
    assert "max_tokens=8192" in src, "改写调用必须显式给 max_tokens"


@pytest.mark.parametrize(
    "src,expect_empty", [("document", False), ("pdf", False), ("wechat_mp", True)]
)
def test_分支表与实现一致(src, expect_empty):
    assert (runner_mod._material_guidance(src) == "") is expect_empty
