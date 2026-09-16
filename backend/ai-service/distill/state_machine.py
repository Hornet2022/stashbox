"""蒸馏任务状态机（v1 §5.2.1 + CP3.5-pre-2）。

状态转换：
    queued → step1_structuring → step2_rewriting → step3_ttsing → step4_concatenating → done
    任意状态 → failed（异常时）
"""
from enum import Enum


class DistillStatus(str, Enum):
    QUEUED = "queued"
    STEP1_STRUCTURING = "step1_structuring"
    STEP2_REWRITING = "step2_rewriting"
    STEP3_TTSING = "step3_ttsing"
    STEP4_CONCATENATING = "step4_concatenating"
    DONE = "done"
    FAILED = "failed"


# 合法转换图
TRANSITIONS: dict[DistillStatus, set[DistillStatus]] = {
    DistillStatus.QUEUED: {DistillStatus.STEP1_STRUCTURING, DistillStatus.FAILED},
    DistillStatus.STEP1_STRUCTURING: {DistillStatus.STEP2_REWRITING, DistillStatus.FAILED},
    DistillStatus.STEP2_REWRITING: {DistillStatus.STEP3_TTSING, DistillStatus.FAILED},
    DistillStatus.STEP3_TTSING: {DistillStatus.STEP4_CONCATENATING, DistillStatus.FAILED},
    DistillStatus.STEP4_CONCATENATING: {DistillStatus.DONE, DistillStatus.FAILED},
    DistillStatus.DONE: set(),
    DistillStatus.FAILED: set(),
}


def can_transition(from_: DistillStatus, to: DistillStatus) -> bool:
    return to in TRANSITIONS[from_]


def transition(from_: DistillStatus, to: DistillStatus) -> DistillStatus:
    if not can_transition(from_, to):
        raise ValueError(f"illegal transition: {from_} → {to}")
    return to
