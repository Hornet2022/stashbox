"""稿子的**四段结构**（2026-10-24 产品拍板后）。

## 产品定义（这里是唯一的权威）

> 「稿子不该是『原文的压缩』，而该是：
> **原文核心要点 + 关联思考 + 延伸判断 + 拓展方向**。」

⇒ 四段是稿子的**结构**，不是可选的加分项。缺一段 = 稿子不完整。

## 这个用例在守什么（一个已经发生过的静默失败）

素材类型分支（`_DOCUMENT_MATERIAL_GUIDANCE` / `_GITHUB_MATERIAL_GUIDANCE`）
是**我自己加的**，不是产品要求。它当时写着：

    「insights（延伸判断）在文档上常常不成立 —— 文档本身没给依据。
      没有依据就给空数组，不要凑数。」

⇒ 文档剪藏与 GitHub 剪藏产出的稿子 `insights` **稳定为 0**，
而那两篇看上去「挺完整」—— 少了四分之一的内容，**没人发现**。

为什么没人发现：质量检查只判**单条**是否合格，空组被
`if not q["total"]: continue` **直接跳过** ⇒ 缺段在日志里**完全静默**。

⇒ 两层护栏：
1. **prompt 层**：追加段不许再教模型「整段留空」；
2. **判据层**：`recreation_quality.missing_parts` 把缺段变成可检测的信号。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_SERVICE_DIR = BACKEND_DIR / "ai-service"
if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))

from agent import runner as runner_mod  # noqa: E402
from agent import recreation_quality  # noqa: E402


# ---------------------------------------------------------------------------
# 1. prompt 层：追加段不许教模型整段留空
# ---------------------------------------------------------------------------
def _insights_clause(guidance: str) -> str:
    """截出提到 insights 的那一条（到下一个编号条目为止）。"""
    start = guidance.find("insights")
    assert start >= 0, "追加段里必须提到 insights"
    tail = guidance[start:]
    # 下一个编号条目（"\\nN. " 形式）就是这一条的结束
    nxt = tail.find("\n8. ")
    if nxt < 0:
        nxt = tail.find("\n5. ")
    return tail[:nxt] if nxt > 0 else tail


def test_文档口径不再教模型放弃延伸判断():
    g = runner_mod._material_guidance("document")
    clause = _insights_clause(g)
    assert "空数组" not in clause, (
        "文档口径又变回「延伸判断可以整段留空」了 —— " "产品定义是四段缺一段就不完整"
    )


def test_github口径不再教模型放弃延伸判断():
    g = runner_mod._material_guidance("github")
    clause = _insights_clause(g)
    assert "空数组" not in clause, (
        "GitHub 口径又变回「延伸判断可以整段留空」了 —— " "产品定义是四段缺一段就不完整"
    )


def test_主prompt声明四段是结构():
    """主 prompt 必须说清：四段齐备，缺一段就不完整。"""
    p = runner_mod._REWRITE_SYSTEM
    assert "这是再创作，不是摘要" in p
    for part in ("sections", "insights", "related", "follow_ups"):
        assert part in p, f"主 prompt 少了 {part}"
    # 「宁缺毋滥」约束的是单条，不是整段
    assert "不要整段留空" in p


def test_主prompt不再允许用推不出就少写绕过整段():
    """旧口径「追不回就少写」会被理解成「可以整段不写」⇒ 已改掉。"""
    p = runner_mod._REWRITE_SYSTEM
    assert (
        "追不回就少写" not in p
    ), "「追不回就少写」这条已被产品口径取代（推不出就换一条，不是整段留空）"


# ---------------------------------------------------------------------------
# 2. 判据层：缺段必须可检测
# ---------------------------------------------------------------------------
def test_四段齐备时没有缺段():
    parts = {
        "sections": ["要点一"],
        "insights": ["延伸判断一"],
        "related": ["关联思考一"],
        "follow_ups": ["拓展方向一"],
    }
    assert recreation_quality.missing_parts(parts) == []


def test_缺延伸判断会被判为缺段():
    """这是**真实发生过的**形状：文档/GitHub 稿的 insights=0。"""
    parts = {
        "sections": ["要点一"],
        "insights": [],
        "related": ["关联思考一"],
        "follow_ups": ["拓展方向一"],
    }
    assert recreation_quality.missing_parts(parts) == ["insights"]


def test_缺多段会全部报出来():
    parts = {"sections": ["要点一"], "insights": [], "related": [], "follow_ups": []}
    assert recreation_quality.missing_parts(parts) == ["insights", "related", "follow_ups"]


def test_必段清单就是产品定义的四段():
    assert recreation_quality.REQUIRED_PARTS == ("sections", "insights", "related", "follow_ups")


def test_缺段会打warning而不是静默():
    """缺段必须**出声**：以前空组被 `if not q["total"]: continue` 跳过。

    ⚠️ 这里断言的是「**用了共享判据**」而不是文本位置 ——
    位置断言会被注释里的同名文本干扰（我自己就踩了一次）。
    真正要守住的是：**判据只有一份**（`recreation_quality.missing_parts`），
    不要在 runner 里另写一份（两份判据必然漂移）。
    """
    src = (AI_SERVICE_DIR / "agent" / "runner.py").read_text(encoding="utf-8")
    assert "agent_rewrite_recreation_missing_part" in src, "缺段必须打一条可检索的日志"
    assert "recreation_quality.missing_parts(" in src, "runner 必须调用共享的缺段判据"
    # 判据**只有一份**：runner 里不许自己再实现一遍「哪些段是必段」
    assert src.count("REQUIRED_PARTS") == 0, "必段清单只在 recreation_quality 里定义一份"
