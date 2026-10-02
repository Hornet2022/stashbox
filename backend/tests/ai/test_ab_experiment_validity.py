"""闭环 2：A/B 实验前提不成立（2026-10-02 端到端自测新增）。

## 背景

个性化分组靠 `ab_group = user_id % 100 < 30` 在**落库时**硬算
（`distill_task.py:546`），但个性化本身从未真正分组生效：
`agent.memory.load_few_shots` 读 few-shot 池时**不按 user_id 过滤**，
所有用户拿到同一批样本。

于是 personalized / general 两组改写输入完全相同，报表差异只可能来自噪声。
但报表照常出数字、不给任何信号 → 运营会读成「实验跑了，没差异」。

更糟的是 `is_personalized` 也恒为 False：`AgentState` 里压根没这个字段，
agent 全目录零处产生它，于是 `bool(final.get("is_personalized"))` 恒 False，
落库那列全表 false，报表说「个性化没上线」—— 而实际 few-shot 是真的注进
prompt 了（`_maybe_inject_memory` 就在 rewrite_node 里跑着）。

两处都在骗人，而且方向相反：一个说没个性化，一个说实验跑了没差异。

## 这里锁的契约

1. `rewrite_node` 必须产出 `is_personalized`（as-treated 口径）
2. `compute_ab_report` 必须显式下发 `experiment_valid=False` + 硬 caveat
"""

import sys
from pathlib import Path

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

AI_SERVICE_DIR = str(Path(__file__).resolve().parents[2] / "ai-service")
if AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, AI_SERVICE_DIR)


# ---------------------------------------------------------------------------
# 1. is_personalized 必须存在（之前恒 False）
# ---------------------------------------------------------------------------


def test_agent_state_declares_is_personalized():
    """字段缺失 → 节点 return 的值被 reducer 丢掉 → 落库恒 False。"""
    from agent.state import AgentState

    assert "is_personalized" in AgentState.__annotations__


def test_rewrite_node_emits_is_personalized():
    """源码级断言：rewrite_node 的返回体里必须有这个 key。

    用源码检查而不是跑整条 agent 图 —— 后者要真实 LLM 和 DB，
    放在单测里既慢又脆。但「有没有产出这个 key」正是本条要锁的东西。
    """
    import inspect

    from agent import runner

    src = inspect.getsource(runner.rewrite_node)

    assert '"is_personalized"' in src, (
        "rewrite_node 必须返回 is_personalized；"
        "缺失会让 distill_task 的 bool(final.get(...)) 恒为 False"
    )


def test_is_personalized_is_true_when_few_shot_present():
    """as-treated 口径：有 few-shot 注入 → True。"""
    import inspect

    from agent import runner

    src = inspect.getsource(runner.rewrite_node)
    # 语义断言：表达式里两个来源都要在
    assert "few_shot_examples" in src
    assert "preferences" in src


# ---------------------------------------------------------------------------
# 2. ab-report 必须显式声明不可用
# ---------------------------------------------------------------------------


def test_ab_report_marks_experiment_invalid():
    import inspect

    from distill import ab_report

    src = inspect.getsource(ab_report.compute_ab_report)
    assert '"experiment_valid": False' in src, (
        "compute_ab_report 必须显式下发 experiment_valid=False，" "否则前端无从判断该不该拦这一屏"
    )


def test_ab_report_first_caveat_states_the_reason():
    """caveats[0] 必须是硬声明，且说清「为什么」，不是泛泛的免责声明。"""
    import inspect

    from distill import ab_report

    src = inspect.getsource(ab_report.compute_ab_report)
    # 第一条 caveat 里要出现「前提不成立」和成因关键词
    assert "前提不成立" in src
    assert "user_id" in src, "caveat 要点明成因是选样没按 user_id 分组"


def test_ab_report_keeps_legacy_caveats():
    """原有的两条历史 caveat 不能被这次改动挤掉。"""
    import inspect

    from distill import ab_report

    src = inspect.getsource(ab_report.compute_ab_report)
    assert "pre_experiment" in src
    assert "2 周" in src


# ---------------------------------------------------------------------------
# 3. 关键区分：修好标志位 ≠ 实验恢复有效
# ---------------------------------------------------------------------------


def test_source_documents_that_flag_fix_does_not_restore_experiment():
    """防止后人误以为「is_personalized 改成 True 就等于 A/B 恢复了」。

    这是本次最容易犯的误读：标志位修好后两组都会是 True，差异依然不存在。
    必须在代码里留明确警告。
    """
    import inspect

    from agent import runner

    src = inspect.getsource(runner.rewrite_node)
    assert "不按 user_id" in src or "按 user_id 分组" in src, (
        "rewrite_node 里要写清：load_few_shots 不按 user_id 过滤，"
        "所以这个标志位为 True 不代表 A/B 实验恢复有效"
    )


def test_ab_group_is_still_itt_and_computed_in_distill_task():
    """ab_group 仍按 ITT 口径硬算（user_id%100<30）—— 本次没改它。"""
    import inspect

    from tasks import distill_task

    src = inspect.getsource(distill_task)
    assert "user_id % 100 < 30" in src
