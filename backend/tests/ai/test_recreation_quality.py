"""再创作内容质量契约的判据测试（2026-10-22 内测反馈「不够专业、深度」）。

fixture 全部取自**生产真实产出**，不是编的：
good/bad 样本见 agent/recreation_quality.py 的 docstring。
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
def quality():
    from agent import recreation_quality as mod

    return mod


# ── 生产真实坏样本（art_8f52de93146b4c45baf1c80e）──────────────────────────
BAD_ITEMS = [
    # 复述原文主旨
    "模型能力与执行基建的脱节是散户最大痛点，未来竞争焦点将从‘谁模型强’转向‘谁数据管道更干净’",
    # 只给「是什么」，不给「所以呢」
    "Bloomberg终端年费近3万美元，QuantGPT本质是AI时代的Bloomberg接口",
    # 定义式标签
    "Alpaca作为券商API层，是AI交易从研究到执行的关键信任锚点",
    # 名词短语（选题标签）
    "MCP协议在交易场景下的安全边界设计，如何防止AI被提示注入后误操作",
    # 名词短语
    "幸存者偏差对量化策略回测的量化影响，以及公开数据源中退市公司覆盖率的差异",
]

# ── 生产真实合格样本 ────────────────────────────────────────────────────
GOOD_ITEMS = [
    "MCP正在成为AI交易基础设施的USB接口，但当前生态仍像早期互联网，标准有了，应用层严重碎片化",
    "QuantGPT的取舍很清楚：它不替用户做决策，只保证算出来的夏普比率真的跑过回测，"
    "这种把不确定性留在系统外的做法，比再聪明的模型更值得抄。",
]


@pytest.mark.parametrize("item", BAD_ITEMS)
def test_bad_items_are_flagged(quality, item):
    """生产里的坏样本必须被判不合格（否则这套判据没用）。"""
    codes = quality.check_item(item, body_text="")
    assert codes, f"应该判为不合格：{item!r}"


@pytest.mark.parametrize("item", GOOD_ITEMS)
def test_good_items_pass(quality, item):
    """合格样本不能被误伤（判据太严会把好内容也砍掉）。"""
    codes = quality.check_item(item, body_text="")
    assert not codes, f"不该被判不合格：{item!r} → {codes}"


def test_list_mode_is_reported_as_a_signal_not_a_defect(quality):
    """「清单模式」（没有句末标点）只当**信号**记，不当缺陷。

    它是 JSON 数组把模型切进清单模式的探针（同一份稿子的 sections 全都有
    句号，这三个字段的条目全都没有），但它本身不是用户抱怨的
    「不专业、不深」—— 所以不能拿它当硬判据，
    更不能靠「给条目补个句号」来刷这个指标。
    """
    profile = quality.check_group(BAD_ITEMS + GOOD_ITEMS, body_text="")
    # 坏样本全部没有句末标点
    for item in BAD_ITEMS:
        assert not item.endswith(("。", "！", "？")), item
    assert profile["signals"].get("no_sentence_end", 0) >= len(BAD_ITEMS)
    # 但它不进 issues（硬缺陷）
    assert "no_sentence_end" not in profile["issues"]


def test_topic_label_pattern_is_detected(quality):
    """名词化的选题短语（「…的设计/的影响/的差异」）要能识别。"""
    assert "topic_label" in quality.check_item(
        "MCP协议在交易场景下的安全边界设计，如何防止AI被提示注入后误操作", ""
    )


def test_imperative_start_is_a_signal_not_a_defect(quality):
    """「以关注/研究开头」只当**信号**记，不当缺陷。

    只有整条都是选题时才是毛病；后面跟着推理的条目即使以「关注…」开头
    也是好内容（实测：「关注'数据清洗'对回测真实性的影响。很多回测漂亮得
    不真实，根子常在数据里没有退市公司。判断一个AI交易工具，先看它有没有
    吃退市的账…」三句话都在讲理）。
    """
    profile = quality.check_group(["关注封闭测试的时间线与反馈，它决定这次功能开发能不能兑现"], "")
    assert profile["signals"].get("imperative_start") == 1
    # 纯选题（没有推理）仍然是硬缺陷
    assert "no_reasoning" in quality.check_item(
        "关注封闭测试的时间线与反馈，它决定这次功能开发能不能兑现", ""
    )


def test_restating_the_body_is_caught_by_missing_reasoning(quality):
    """把原文主旨换个说法复述，要能识别（这正是「不是再创作」）。

    抓它的是 `no_reasoning`：纯复述没有「因为/所以/但」这类推理。
    """
    body = (
        "对冲基金赢在基建而非天才。它们拥有几十年干净数据、7×24研究集群和永不下线的执行系统。"
        "模型能力与执行基建的脱节是散户最大痛点。"
    )
    item = "模型能力与执行基建的脱节是散户最大痛点，未来竞争焦点将从谁模型强转向谁数据管道更干净。"
    assert "no_reasoning" in quality.check_item(item, body)


def test_overlap_ratio_is_a_signal_not_a_defect(quality):
    """与正文的重合度**不能**当硬判据（实测与质量反相关）。

    prompt 要求「推论要能追回原文的某个事实」→ 好条目必然与正文共享词汇。
    实测（同一篇原文）：

        旧 prompt 的坏样本   重合度 0.154 ~ 0.333
        新 prompt 的好样本   重合度 > 0.34

    也就是说重合度高的反而是好内容。这条判据当年差点把「延伸判断」
    这个产品定位本身给砍掉 —— 所以只能当信号。
    """
    body = "对冲基金赢在基建而非天才，它们拥有几十年干净数据、7×24研究集群和永不下线的执行系统。"
    good = (
        "QuantGPT的取舍很清楚：它不替用户做决策，只保证算出来的夏普比率真的跑过回测，"
        "这种把不确定性留在系统外的做法，比再聪明的模型更值得抄。"
    )
    assert "restates_body" not in quality.check_item(good, body)


def test_original_insight_is_not_flagged_as_restatement(quality):
    """真正的新推论不该被误判成「复述正文」。

    这条很重要：判据太狠会把「延伸判断」这个产品定位本身给砍掉。
    """
    body = (
        "QuantGPT把基金机房搬进AI：数据层每晚刷新，含退市公司和真实公布日基本面；"
        "计算层AI只发规则，服务器跑回测。"
    )
    item = (
        "这套把回测交给服务器的设计，等于让AI从‘猜方向’退回‘执行策略’——"
        "听众真正该记住的也许不是哪个模型更聪明，而是谁的基础设施更干净。"
    )
    assert "restates_body" not in quality.check_item(item, body)


def test_group_profile_reports_counts(quality):
    """分组画像要能直接回答「有多少条不合格、分别什么毛病」。"""
    profile = quality.check_group(BAD_ITEMS + GOOD_ITEMS, body_text="")
    assert profile["total"] == len(BAD_ITEMS) + len(GOOD_ITEMS)
    assert profile["ok"] == len(GOOD_ITEMS)
    assert profile["issues"], "应该统计出各类问题"
    assert len(profile["offenders"]) == len(BAD_ITEMS)
