"""蒸馏状态机测试（任务包 §4.3）。"""
import pytest

from distill import DistillStatus
from distill.state_machine import TRANSITIONS, can_transition, transition

PROGRESS_CHAIN = [
    (DistillStatus.QUEUED, DistillStatus.STEP1_STRUCTURING),
    (DistillStatus.STEP1_STRUCTURING, DistillStatus.STEP2_REWRITING),
    (DistillStatus.STEP2_REWRITING, DistillStatus.STEP3_TTSING),
    (DistillStatus.STEP3_TTSING, DistillStatus.STEP4_CONCATENATING),
    (DistillStatus.STEP4_CONCATENATING, DistillStatus.DONE),
]

NON_TERMINAL = [
    DistillStatus.QUEUED,
    DistillStatus.STEP1_STRUCTURING,
    DistillStatus.STEP2_REWRITING,
    DistillStatus.STEP3_TTSING,
    DistillStatus.STEP4_CONCATENATING,
]


@pytest.mark.parametrize("from_,to", PROGRESS_CHAIN)
def test_legal_progress_transitions(from_, to):
    assert can_transition(from_, to) is True
    assert transition(from_, to) is to


@pytest.mark.parametrize("from_", NON_TERMINAL)
def test_any_non_terminal_can_fail(from_):
    assert can_transition(from_, DistillStatus.FAILED) is True


def test_illegal_transition_raises_value_error():
    with pytest.raises(ValueError, match="illegal transition"):
        transition(DistillStatus.DONE, DistillStatus.STEP1_STRUCTURING)


def test_illegal_transition_message_contains_both_states():
    with pytest.raises(ValueError) as exc:
        transition(DistillStatus.QUEUED, DistillStatus.DONE)
    message = str(exc.value)
    assert "QUEUED" in message
    assert "DONE" in message


def test_cannot_skip_steps():
    """QUEUED 不能直接跳到 STEP3。"""
    assert can_transition(DistillStatus.QUEUED, DistillStatus.STEP3_TTSING) is False
    with pytest.raises(ValueError):
        transition(DistillStatus.QUEUED, DistillStatus.STEP3_TTSING)


def test_cannot_go_backwards():
    assert can_transition(DistillStatus.STEP2_REWRITING, DistillStatus.STEP1_STRUCTURING) is False


def test_done_is_terminal():
    assert TRANSITIONS[DistillStatus.DONE] == set()
    assert can_transition(DistillStatus.DONE, DistillStatus.FAILED) is False


def test_failed_is_terminal():
    assert TRANSITIONS[DistillStatus.FAILED] == set()
    assert can_transition(DistillStatus.FAILED, DistillStatus.QUEUED) is False
    assert can_transition(DistillStatus.FAILED, DistillStatus.STEP1_STRUCTURING) is False


def test_transition_graph_covers_every_status():
    assert set(TRANSITIONS) == set(DistillStatus)


def test_status_values_are_strings():
    """DistillStatus 是 str Enum —— 可以直接写进 DB 的 String 列。"""
    assert DistillStatus.STEP1_STRUCTURING == "step1_structuring"
    assert DistillStatus.DONE.value == "done"
    assert isinstance(DistillStatus.QUEUED, str)


def test_transition_returns_target_status():
    """transition 返回目标状态（流水线靠返回值推进 self._current）。"""
    assert transition(DistillStatus.STEP3_TTSING, DistillStatus.FAILED) is DistillStatus.FAILED
