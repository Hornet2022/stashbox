"""「更深刻、更丰富」的判据与补救（2026-10-25 产品反馈：内容不够深刻、篇幅不够）。

## 背景：为什么是「加条数 + 补救」，不是「喊得更用力」

实测（生产两篇原文，新旧规格各跑一次，生产真实调用路径）：

    旧规格：整稿 1308 字，related/insights/follow_ups = 2/3/2，判据合格 7/7
    新规格：整稿 1866 字，related/insights/follow_ups = 3/4/3，判据合格 8/10

⇒ 篇幅确实涨了（+43%），但**多出来的条目里混进了选题短语**
（「关注X的部署进度」「追踪Y的里程碑」——`no_reasoning`）。

⇒ 所以「写更多」必须配上「把不合格的换掉」，否则只是把「浅」一起放大。
本文件锁住这两件事：

1. **丰度判据**（条数下限 / 分段字数下限）—— 机械可判定，不靠肉眼。
2. **补救**（局部重写不合格条目 + 补齐条数不够的段）—— 只记不丢。

fixture 全部取自**生产真实产出**（2026-10-25 新规格探针的两条弱条目）。
"""

import json
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
if str(AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE))


# ── 生产真实弱条目（2026-10-25 新规格探针，art_a7f9ed74f5114f3ba9c15236）──────
WEAK_FOLLOW_UPS = [
    "关注Gemini Agent在Gmail和搜索中的实际部署进度，以及用户反馈。这是多Agent系统落地的第一战场，"
    "其体验将决定个人Agent的普及速度。",
    "追踪TPU太空测试的里程碑进展，特别是从次同步轨道传回数据的时间点。这是太空数据中心从概念"
    "走向现实的关键节点，可能引发新一轮基础设施投资。",
]

# ── 合格条目（带推论 + 具体）──────────────────────────────────────────────
GOOD_FOLLOW_UP = (
    "Gemini Agent 能不能落地，关键不在模型答得多好，而在它敢不敢替用户按下发送键——"
    "这一步跨过去，个人助理才算真的开始替人干活。"
)

#: ≥60 字（related 的分段下限）且带推论的合格条目
GOOD_RELATED = (
    "它和另一条技术路线其实是同一类问题的两种解法：一条把成本压到推理侧，一条把成本压到"
    "部署侧，因为两者面对的用户规模根本不同，所以取舍也就完全相反。"
)
GOOD_INSIGHT = (
    "真正的瓶颈大概率不在模型能力，而在每一次调用背后那套没人愿意维护的数据管道，"
    "因为数据口径一变，前面的推理全部作废，所以换模型的收益远小于把管道理顺，"
    "这也是为什么同样一件事，有人三个月做不完，有人一周就能跑通。"
)
GOOD_INSIGHT_2 = (
    "更该担心的不是它做错，而是它做错之后没人发现，因为自动化程度越高，"
    "错误就越难被人察觉，所以真正的分水岭是能不能把中间结果摊开给人看，"
    "而不是把最后的答案直接甩到脸上。"
)


@pytest.fixture
def quality():
    from agent import recreation_quality as mod

    return mod


@pytest.fixture
def runner():
    from agent import runner as mod

    return mod


# ---------------------------------------------------------------------------
# 1. 丰度判据
# ---------------------------------------------------------------------------


def test_underfilled_detects_short_counts(quality):
    """条数不够要能判出来（段在，但内容不够）。"""
    under = quality.underfilled(
        {"related": ["一条"], "insights": ["一", "二", "三"], "follow_ups": []}
    )
    assert "related" in under
    assert "follow_ups" in under
    assert "insights" not in under, "insights 够 3 条就不该被判不足"


def test_underfilled_is_empty_when_all_floors_met(quality):
    parts = {
        "related": ["a", "b"],
        "insights": ["a", "b", "c"],
        "follow_ups": ["a", "b"],
    }
    assert quality.underfilled(parts) == {}


def test_part_floor_is_stricter_than_global_floor(quality):
    """分段字数下限比全局 40 字严：过得了全局、过不了 related(60) 与 insights(70)。"""
    item = (
        "关键在于它敢不敢替用户按下发送键这一步，因为只有真的代办才算落地，"
        "不做的话它永远只是个问答"
    )
    assert 40 <= len(item) < quality.MIN_CHARS_BY_PART["related"]
    assert "too_short" not in quality.check_item(item, "")
    assert "too_short" in quality.check_item_for_part(item, "related", "")
    assert "too_short" in quality.check_item_for_part(item, "insights", "")


def test_weak_items_reports_offenders_with_codes(quality):
    weak = quality.weak_items({"follow_ups": WEAK_FOLLOW_UPS}, "")
    assert "follow_ups" in weak
    assert len(weak["follow_ups"]) == 2
    for _item, codes in weak["follow_ups"]:
        assert "no_reasoning" in codes


def test_weak_items_empty_when_all_good(quality):
    assert quality.weak_items({"follow_ups": [GOOD_FOLLOW_UP]}, "") == {}


# ---------------------------------------------------------------------------
# 2. 补救的合并规则
# ---------------------------------------------------------------------------


def _parts(runner, **kw):
    from agent.runner import _RewriteParts

    base = dict(
        hook="钩子。",
        sections=["要点一。", "要点二。"],
        outro="收束。",
        insights=[GOOD_INSIGHT, "判断二，但是呢。"],
        related=[GOOD_RELATED],
        follow_ups=list(WEAK_FOLLOW_UPS),
        body_sections=["要点一。", "要点二。"],
    )
    base.update(kw)
    return _RewriteParts(**base)


def test_apply_remediation_replaces_weak_items_in_place(runner):
    p = _parts(runner)
    weak = runner.recreation_quality.weak_items(
        {"related": p.related, "insights": p.insights, "follow_ups": p.follow_ups}, ""
    )
    runner._apply_remediation(p, {"follow_ups": [GOOD_FOLLOW_UP]}, weak)
    assert p.follow_ups[0] == GOOD_FOLLOW_UP
    assert len(p.follow_ups) == 2, "只给了一个替补 ⇒ 第二个保留原条目（只记不丢）"
    assert p.follow_ups[1] == WEAK_FOLLOW_UPS[1]


def test_apply_remediation_keeps_original_when_no_replacement(runner):
    """替补用完 ⇒ 保留原条目，**不许丢内容**（删内容比内容浅更糟）。"""
    p = _parts(runner)
    weak = runner.recreation_quality.weak_items(
        {"related": p.related, "insights": p.insights, "follow_ups": p.follow_ups}, ""
    )
    runner._apply_remediation(p, {}, weak)
    assert p.follow_ups == WEAK_FOLLOW_UPS


def test_apply_remediation_appends_when_only_underfilled(runner):
    """只是条数不够（没弱条目）⇒ 追加，不替换。"""
    p = _parts(runner, follow_ups=[GOOD_FOLLOW_UP], related=[GOOD_RELATED])
    weak = runner.recreation_quality.weak_items(
        {"related": p.related, "insights": p.insights, "follow_ups": p.follow_ups}, ""
    )
    runner._apply_remediation(
        p, {"related": ["新增的关联，因为所以，所以这里补一条足够长的内容。"]}, weak
    )
    assert len(p.related) == 2
    assert p.related[-1].startswith("新增的关联")


def test_apply_remediation_rebuilds_sections_in_product_order(runner):
    """sections 必须重建为「正文要点 → 关联思考 → 延伸判断 → 拓展方向」。"""
    p = _parts(runner)
    weak = runner.recreation_quality.weak_items(
        {"related": p.related, "insights": p.insights, "follow_ups": p.follow_ups}, ""
    )
    runner._apply_remediation(p, {"follow_ups": [GOOD_FOLLOW_UP]}, weak)
    assert p.sections == list(p.body_sections) + list(p.related) + list(p.insights) + list(
        p.follow_ups
    )


# ---------------------------------------------------------------------------
# 3. 补救调用本身
# ---------------------------------------------------------------------------


def _fake_chat(payload):
    async def _chat_text(llm, user_prompt, *, system_prompt=None, step="", **kw):
        _fake_chat.calls.append({"prompt": user_prompt, "system": system_prompt})
        return payload

    _fake_chat.calls = []
    return _chat_text


@pytest.mark.asyncio
async def test_remediate_recreation_returns_accepted_items(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_chat_text",
        _fake_chat(json.dumps({"follow_ups": [GOOD_FOLLOW_UP]}, ensure_ascii=False)),
    )
    p = _parts(runner)
    accepted = await runner._remediate_recreation(runner, p, "", "原文")
    assert accepted["follow_ups"] == [GOOD_FOLLOW_UP]


@pytest.mark.asyncio
async def test_remediate_recreation_rejects_still_bad_items(runner, monkeypatch):
    """重写后**仍然不合格**的条目不许进稿（否则补救只是换了个写法）。"""
    monkeypatch.setattr(
        runner,
        "_chat_text",
        _fake_chat(json.dumps({"follow_ups": WEAK_FOLLOW_UPS}, ensure_ascii=False)),
    )
    p = _parts(runner)
    accepted = await runner._remediate_recreation(runner, p, "", "原文")
    assert accepted == {}


@pytest.mark.asyncio
async def test_remediate_recreation_returns_empty_when_nothing_to_fix(runner, monkeypatch):
    """没有弱条目、条数也够 ⇒ 不该白花一次 LLM 调用。"""
    called = []

    async def _boom(llm, user_prompt, *, system_prompt=None, step="", **kw):
        called.append(1)
        return "{}"

    monkeypatch.setattr(runner, "_chat_text", _boom)
    p = _parts(
        runner,
        follow_ups=[GOOD_FOLLOW_UP, GOOD_RELATED],
        related=[GOOD_RELATED, GOOD_INSIGHT],
        insights=[GOOD_INSIGHT, GOOD_RELATED, GOOD_INSIGHT_2],
    )
    accepted = await runner._remediate_recreation(runner, p, "", "原文")
    assert accepted == {}
    assert not called, "没问题可改就不该调 LLM"


@pytest.mark.asyncio
async def test_remediate_recreation_survives_llm_failure(runner, monkeypatch):
    """补救失败不许拖垮整篇稿（只记不丢 ⇒ 原稿照用）。"""

    async def _boom(llm, user_prompt, *, system_prompt=None, step="", **kw):
        raise RuntimeError("上游 502")

    monkeypatch.setattr(runner, "_chat_text", _boom)
    p = _parts(runner)
    accepted = await runner._remediate_recreation(runner, p, "", "原文")
    assert accepted == {}


@pytest.mark.asyncio
async def test_remediate_recreation_survives_unparsable_output(runner, monkeypatch):
    """LLM 没按 JSON 输出 ⇒ 不崩，当作没修成。"""
    monkeypatch.setattr(runner, "_chat_text", _fake_chat("好的，我来改写一下"))
    p = _parts(runner)
    accepted = await runner._remediate_recreation(runner, p, "", "原文")
    assert accepted == {}


# ---------------------------------------------------------------------------
# 4. 规格不许悄悄退回旧值
# ---------------------------------------------------------------------------


def test_rewrite_prompt_spec_has_new_length_tiers(runner):
    """篇幅阶梯是「更丰富」的闸门 —— 退回旧值 = 需求被静默撤销。"""
    assert "1900-2600 字" in runner._REWRITE_SYSTEM
    assert "1400-1900 字" in runner._REWRITE_SYSTEM
    assert "900-1200 字" in runner._REWRITE_SYSTEM
    assert "1000-1400 字" not in runner._REWRITE_SYSTEM, "旧的闸门值不该还在"


def test_rewrite_prompt_spec_has_new_min_counts(runner):
    assert '"related": ["关联思考，2-3 条' in runner._REWRITE_SYSTEM
    assert '"insights": ["延伸判断，3-4 条' in runner._REWRITE_SYSTEM
    assert '"follow_ups": ["拓展方向，2-3 条' in runner._REWRITE_SYSTEM
