"""改写稿的**结构保全**守卫（2026-11-25）。

## 为什么要有这份测试

内测反馈：「内容很浅薄」。调研（95 篇生产稿子）发现两个机制性原因：

1. **结构丢失**：LLM 输出被 `max_tokens` 截断 ⇒ JSON 不完整 ⇒ 解析失败 ⇒
   走降级路径（按 `\\n\\n` 切段）⇒ 而 JSON 缩进是**单换行**，切不开 ⇒
   整个 JSON 变成 1 段 ⇒ **正文要点 + 三段再创作全部丢失**
   （生产实测：95 篇里 1 篇四段全空）。

   ⇒ 修法：① 完整解析失败时**逐元素抢救**已写完的部分；
     ② 降级稿**也要补救**（降级恰恰是最需要补救的场景）。

2. 深度不足 —— 见 `test_recreation_quality.py` 的 `topic_label_open`。

## 这份测试钉住什么

- 截断的 JSON 能被**抢救**出已完成的部分；
- 抢救成功 ⇒ **不许再落进降级路径**（三段不能白救一场）；
- 降级稿会**触发补救**（且不按整稿字数补，避免一口气要十几条）。
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
if str(AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE))


@pytest.fixture
def runner():
    from agent import runner as mod

    return mod


# ── 生产真实样本（art_4aee39aafb27406ca1424887 的降级稿）──────────────────
#
# 这是**被 max_tokens 截断**的输出：hook + sections 写完了，
# outro / related / insights / follow_ups 还没轮到就断了。
TRUNCATED_RAW = """{
  "hook": "把网站加载时间从10秒压到5秒？直接告诉AI目标定到0.2秒。模型说做不到？再告诉它：这非常合理。",
  "sections": [
    "Felix Rieseberg是Claude Code桌面版负责人，他分享了一套AI使用技巧，核心是'更有野心'。",
    "高度：从完成任务到实现目标。比如做季度汇报PPT，别只填数据，而是让模型理解业务变化。"
  ],
  "outro": "先定一个不合理的目"""


def test_salvage_recovers_completed_parts(runner):
    """截断的 JSON：已写完的字段必须救回来。"""
    obj = runner._salvage_json_object(TRUNCATED_RAW)
    assert obj is not None, "应该救回一部分"
    assert obj["hook"].startswith("把网站加载时间")
    assert len(obj["sections"]) == 2
    assert obj["sections"][0].startswith("Felix Rieseberg")
    # 没写完的字段不强求（截断在那里）
    assert "insights" not in obj


def test_salvage_keeps_only_complete_array_items(runner):
    """数组里没闭合的那一条要丢掉，前面的完整条目要保住。"""
    raw = """{"sections": [
    "第一条完整的句子。",
    "第二条完整的句子。",
    "第三条被截断了，没有闭合"""
    obj = runner._salvage_json_object(raw)
    assert obj is not None
    assert obj["sections"] == ["第一条完整的句子。", "第二条完整的句子。"]


def test_truncated_json_does_not_fall_into_degraded_path(runner):
    """⚠️ **核心守卫**：抢救成功 ⇒ 绝不能落进降级路径。

    为什么这条最重要：降级路径按 `\\n\\n` 切段，而 JSON 缩进是**单换行**
    ⇒ 切不开 ⇒ 整个 JSON 变成 1 段 ⇒ **三段再创作内容又全丢了**
    （白救一场）。
    """
    parts = runner._parse_rewrite(TRUNCATED_RAW)
    assert not parts.degraded, "抢救成功的稿子不该被标成降级"
    assert parts.hook
    assert len(parts.sections) == 2


def test_salvage_only_recovers_recreation_parts_even_without_body(runner):
    """正文要点正好被截断时，**三段再创作内容也要保住**。

    条件里必须包含三段 —— 否则「只救回三段」会被踢进降级路径。
    """
    raw = '''{"hook": "开场。", "sections": ["正文第一节被截断了""", "related": [
    "关联思考写完了：这件事和另一件事互相印证，所以值得单独说。"
  ]}'''
    parts = runner._parse_rewrite(raw)
    assert not parts.degraded
    assert parts.related, "只救回三段时也必须保住"
    assert parts.related[0].startswith("关联思考写完了")


def test_unparsable_non_json_still_degrades(runner):
    """真的不是 JSON（纯文本）⇒ 老老实实降级，别乱抢救。"""
    raw = "这是一段普通文本。\n\n这是第二段。\n\n第三段。"
    parts = runner._parse_rewrite(raw)
    assert parts.degraded
    assert parts.sections, "纯文本降级应该按空行切出正文"


def test_remediate_supports_skip_chars_gap(runner):
    """补救必须支持「不按整稿字数补」——降级稿的正文要点不可信。

    字数下限是「四段合计」⇒ 正文要点坏掉时缺口会被算得很大
    ⇒ 一口气要求补十几条（实测会触发这种误补）。
    """
    import inspect

    sig = inspect.signature(runner._remediate_recreation)
    assert "skip_chars_gap" in sig.parameters
    assert sig.parameters["skip_chars_gap"].default is False
