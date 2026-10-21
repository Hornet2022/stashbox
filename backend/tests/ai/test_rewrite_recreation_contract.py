"""播客稿的「再创作」契约：LLM 返回的额外字段必须**合进稿子**，不能丢掉。

## 背景

产品定位是「基于原文的再创作」，不是「原文的压缩」：核心要点 + 关联思考 +
延伸判断 + 值得关联的关键点 + 可拓展关注的方向。

第一版把这条要求**写在 prompt 里祈祷**（"融进 sections 里"）—— 实测落地率不稳：
关键词命中只有关联思考 7/11 篇、拓展方向 5/11 篇，LLM 经常偷懒只做摘要。

第二版改成**结构化契约**：JSON 里显式给 `insights` / `related` / `follow_ups`
三个字段。那么 `_parse_rewrite` 就**必须**把它们合进 sections ——
否则就变成「静默丢弃」，比不做还糟（稿子看起来完整，其实少了一半内容）。

## 顺序

hook → sections → insights → related → follow_ups → outro
（拓展方向在收尾之前，逻辑上通：先给方向再收束）
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


@pytest.fixture(scope="module")
def runner():
    import importlib

    if "agent" not in sys.modules:
        importlib.import_module("agent")
    return importlib.import_module("agent.runner")


def test_extra_fields_are_merged_into_sections(runner):
    """insights/related/follow_ups 必须进稿子。"""
    raw = (
        '{"hook": "钩子", "sections": ["正文一", "正文二"], '
        '"insights": ["关联思考一条", "延伸判断一条"], '
        '"related": ["值得关联的一条"], '
        '"follow_ups": ["拓展方向一条"], '
        '"outro": "收束"}'
    )
    parts = runner._parse_rewrite(raw)
    script = parts.script

    assert "钩子" in script
    assert "正文一" in script
    assert "收束" in script
    assert "关联思考一条" in script, "insights 被丢了 —— 稿子会少一半内容"
    assert "延伸判断一条" in script
    assert "值得关联的一条" in script
    assert "拓展方向一条" in script


def test_extra_fields_keep_their_order(runner):
    """顺序：正文 → 关联思考 → 关联关键点 → 拓展方向 → 收束。"""
    raw = (
        '{"hook": "H", "sections": ["S1"], "insights": ["I1"], '
        '"related": ["R1"], "follow_ups": ["F1"], "outro": "O"}'
    )
    script = runner._parse_rewrite(raw).script
    pos = {
        k: script.index(v)
        for k, v in {"H": "H", "S1": "S1", "I1": "I1", "R1": "R1", "F1": "F1", "O": "O"}.items()
    }
    assert pos["H"] < pos["S1"] < pos["I1"] < pos["R1"] < pos["F1"] < pos["O"], pos


def test_missing_extra_fields_is_fine(runner):
    """LLM 没给额外字段（不听话）→ 降级但不崩，稿子照常出。"""
    raw = '{"hook": "钩子", "sections": ["正文"], "outro": "收束"}'
    parts = runner._parse_rewrite(raw)
    assert "钩子" in parts.script
    assert "正文" in parts.script


def test_empty_related_array_is_not_noise(runner):
    """related 给了空数组 → 不产生空段。"""
    raw = '{"hook": "H", "sections": ["S"], "related": [], "outro": "O"}'
    script = runner._parse_rewrite(raw).script
    assert script.strip().count("\n\n") == 2, repr(script)  # H / S / O 三段


def test_extra_field_as_string_is_accepted(runner):
    """LLM 把数组写成字符串 → 也要收进来。"""
    raw = '{"hook": "H", "sections": ["S"], "insights": "单独一条思考", "outro": "O"}'
    assert "单独一条思考" in runner._parse_rewrite(raw).script


def test_prompt_contract_declares_the_three_fields(runner):
    """system prompt 必须声明这三个字段 —— 否则 LLM 根本不会产出。"""
    system = runner._REWRITE_SYSTEM
    for field in ("insights", "related", "follow_ups"):
        assert f'"{field}"' in system, f"system prompt 缺少 {field} 的契约"


def test_prompt_says_extra_fields_are_part_of_the_script(runner):
    """prompt 必须说清这三类内容**会被念给听众听**。

    否则 LLM 会把它们写成"关联：xxx"这种标签式的内容，念出来很怪。
    """
    system = runner._REWRITE_SYSTEM
    assert "会被念给听众听" in system
    assert "不要硬编" in system
