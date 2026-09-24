"""CP5.6.1：冷启动策略 + 评分请求推送 + 自动触发个性化单测。

20 个用例覆盖：
- cold_start_tracker: get_state (3 状态) + should_prompt_rating (4 档)
- rating_prompt: intensity 4 档 + should_show_prompt (限频)
- auto_personalize_trigger: check_threshold (跨过门槛) + mark_enabled
- ColdStartState Pydantic schema
- Pipeline 集成: default_post_step_hooks 不变
"""

import sys
from datetime import datetime, timedelta  # noqa: F401
from pathlib import Path

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# 1. cold_start_tracker - 3 状态
# ---------------------------------------------------------------------------
async def test_get_state_fresh_user():
    """CP5.6.1：feedback_count=0 → fresh 状态。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    class _EmptySession:
        async def scalar(self, *args, **kwargs):
            return None

    tracker = ColdStartTracker()
    state = await tracker.get_state(_EmptySession(), user_id=1)
    assert isinstance(state, ColdStartState)
    assert state.feedback_count == 0
    assert state.state == "fresh"
    assert state.ratings_remaining_to_personalize == 5


async def test_get_state_warming_user():
    """CP5.6.1：feedback_count=3 → warming 状态。"""
    from distill.cold_start_tracker import ColdStartTracker

    class _StubPattern:
        feedback_count = 3

    class _Session:
        async def scalar(self, *args, **kwargs):
            return _StubPattern()

    tracker = ColdStartTracker()
    state = await tracker.get_state(_Session(), user_id=2)
    assert state.feedback_count == 3
    assert state.state == "warming"
    assert state.ratings_remaining_to_personalize == 2


async def test_get_state_active_user():
    """CP5.6.1：feedback_count=10 → active 状态。"""
    from distill.cold_start_tracker import ColdStartTracker

    class _StubPattern:
        feedback_count = 10

    class _Session:
        async def scalar(self, *args, **kwargs):
            return _StubPattern()

    tracker = ColdStartTracker()
    state = await tracker.get_state(_Session(), user_id=3)
    assert state.feedback_count == 10
    assert state.state == "active"
    assert state.ratings_remaining_to_personalize == 0


async def test_get_state_failure_fallback_fresh():
    """CP5.6.1：DB 异常 → 返 fresh fallback。"""
    from distill.cold_start_tracker import ColdStartTracker

    class _BrokenSession:
        async def scalar(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

    tracker = ColdStartTracker()
    state = await tracker.get_state(_BrokenSession(), user_id=4)
    assert state.state == "fresh"
    assert state.feedback_count == 0


# ---------------------------------------------------------------------------
# 2. cold_start_tracker - helpers
# ---------------------------------------------------------------------------
def test_is_fresh_warming_active_helpers():
    """CP5.6.1：is_fresh/is_warming/is_active 状态判定。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    tracker = ColdStartTracker()
    s_fresh = ColdStartState(feedback_count=0, state="fresh", ratings_remaining_to_personalize=5)
    s_warm = ColdStartState(feedback_count=3, state="warming", ratings_remaining_to_personalize=2)
    s_active = ColdStartState(feedback_count=10, state="active", ratings_remaining_to_personalize=0)

    assert tracker.is_fresh(s_fresh) is True
    assert tracker.is_warming(s_warm) is True
    assert tracker.is_active(s_active) is True
    assert tracker.is_fresh(s_warm) is False
    assert tracker.is_active(s_fresh) is False


# ---------------------------------------------------------------------------
# 3. cold_start_tracker - should_prompt_rating
# ---------------------------------------------------------------------------
def test_should_prompt_rating_fresh_first_3():
    """CP5.6.1：fresh + action_count<=3 → True（首 3 篇强推）。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    tracker = ColdStartTracker()
    s = ColdStartState(feedback_count=0, state="fresh", ratings_remaining_to_personalize=5)
    assert tracker.should_prompt_rating(s, user_action_count=1) is True
    assert tracker.should_prompt_rating(s, user_action_count=2) is True
    assert tracker.should_prompt_rating(s, user_action_count=3) is True
    assert tracker.should_prompt_rating(s, user_action_count=4) is False


def test_should_prompt_rating_warming():
    """CP5.6.1：warming + ratings_remaining > 0 → True。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    tracker = ColdStartTracker()
    s = ColdStartState(feedback_count=3, state="warming", ratings_remaining_to_personalize=2)
    assert tracker.should_prompt_rating(s, user_action_count=1) is True


def test_should_prompt_rating_active_under_20():
    """CP5.6.1：active + count < 20 → True（弱推）。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    tracker = ColdStartTracker()
    s = ColdStartState(feedback_count=10, state="active", ratings_remaining_to_personalize=0)
    assert tracker.should_prompt_rating(s, user_action_count=1) is True


def test_should_prompt_rating_active_over_20():
    """CP5.6.1：active + count >= 20 → False（停止打扰）。"""
    from distill.cold_start_tracker import ColdStartTracker
    from distill.schemas import ColdStartState

    tracker = ColdStartTracker()
    s = ColdStartState(feedback_count=20, state="active", ratings_remaining_to_personalize=0)
    assert tracker.should_prompt_rating(s, user_action_count=1) is False


# ---------------------------------------------------------------------------
# 4. rating_prompt - intensity 4 档
# ---------------------------------------------------------------------------
def test_intensity_strong_for_count_0_to_2():
    """CP5.6.1：count 0-2 → strong。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.get_prompt_intensity(0) == "strong"
    assert s.get_prompt_intensity(1) == "strong"
    assert s.get_prompt_intensity(2) == "strong"


def test_intensity_medium_for_count_3_to_4():
    """CP5.6.1：count 3-4 → medium。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.get_prompt_intensity(3) == "medium"
    assert s.get_prompt_intensity(4) == "medium"


def test_intensity_weak_for_count_5_to_19():
    """CP5.6.1：count 5-19 → weak。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.get_prompt_intensity(5) == "weak"
    assert s.get_prompt_intensity(19) == "weak"


def test_intensity_none_for_count_20_plus():
    """CP5.6.1：count >= 20 → none。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.get_prompt_intensity(20) == "none"
    assert s.get_prompt_intensity(50) == "none"


# ---------------------------------------------------------------------------
# 5. rating_prompt - should_show_prompt (限频)
# ---------------------------------------------------------------------------
def test_should_show_prompt_first_time():
    """CP5.6.1：last_prompt_at=None → True（首次）。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.should_show_prompt(0) is True
    assert s.should_show_prompt(4) is True


def test_should_show_prompt_rate_limit():
    """CP5.6.1：距上次 < 24h → False（限频）。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    now = datetime.now()

    # 25h ago → True
    past_far = now - timedelta(hours=25)
    assert s.should_show_prompt(10, last_prompt_at=past_far, now=now) is True

    # 1h ago → False
    past_close = now - timedelta(hours=1)
    assert s.should_show_prompt(10, last_prompt_at=past_close, now=now) is False


def test_should_show_prompt_count_20_plus_never():
    """CP5.6.1：count >= 20 → 永远不推。"""
    from distill.rating_prompt import RatingPromptStrategy

    s = RatingPromptStrategy()
    assert s.should_show_prompt(20) is False
    assert s.should_show_prompt(100) is False


# ---------------------------------------------------------------------------
# 6. auto_personalize_trigger - check_threshold
# ---------------------------------------------------------------------------
async def test_check_threshold_crosses_to_5():
    """CP5.6.1：old=4 → new=5 → True（刚跨过门槛）。"""
    from distill.auto_personalize_trigger import AutoPersonalizeTrigger

    class _StubPattern:
        feedback_count = 4

    class _Session:
        async def scalar(self, *args, **kwargs):
            return _StubPattern()

    trigger = AutoPersonalizeTrigger()
    crossed = await trigger.check_threshold(_Session(), user_id=1, new_feedback_count=5)
    assert crossed is True


async def test_check_threshold_already_active_returns_false():
    """CP5.6.1：old=6 → new=7 → False（已激活）。"""
    from distill.auto_personalize_trigger import AutoPersonalizeTrigger

    class _StubPattern:
        feedback_count = 6

    class _Session:
        async def scalar(self, *args, **kwargs):
            return _StubPattern()

    trigger = AutoPersonalizeTrigger()
    crossed = await trigger.check_threshold(_Session(), user_id=2, new_feedback_count=7)
    assert crossed is False


async def test_check_threshold_below_5_returns_false():
    """CP5.6.1：old=2 → new=3 → False（未到门槛）。"""
    from distill.auto_personalize_trigger import AutoPersonalizeTrigger

    class _StubPattern:
        feedback_count = 2

    class _Session:
        async def scalar(self, *args, **kwargs):
            return _StubPattern()

    trigger = AutoPersonalizeTrigger()
    crossed = await trigger.check_threshold(_Session(), user_id=3, new_feedback_count=3)
    assert crossed is False


async def test_check_threshold_no_pattern_returns_true_at_5():
    """CP5.6.1：no pattern + new=5 → True（新用户首次过门槛）。"""
    from distill.auto_personalize_trigger import AutoPersonalizeTrigger

    class _EmptySession:
        async def scalar(self, *args, **kwargs):
            return None

    trigger = AutoPersonalizeTrigger()
    crossed = await trigger.check_threshold(_EmptySession(), user_id=4, new_feedback_count=5)
    assert crossed is True


# ---------------------------------------------------------------------------
# 7. auto_personalize_trigger - mark_personalization_enabled
# ---------------------------------------------------------------------------
async def test_mark_personalization_enabled_failure_returns_false():
    """CP5.6.1：DB 异常 → False。"""
    from distill.auto_personalize_trigger import AutoPersonalizeTrigger

    class _BrokenSession:
        async def scalar(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def commit(self):
            raise RuntimeError("simulated db error")

        async def rollback(self):
            pass

    trigger = AutoPersonalizeTrigger()
    ok = await trigger.mark_personalization_enabled(_BrokenSession(), user_id=99)
    assert ok is False


# ---------------------------------------------------------------------------
# 8. Pydantic schema
# ---------------------------------------------------------------------------
def test_cold_start_state_schema():
    """CP5.6.1：ColdStartState Pydantic schema 字段正确。"""
    from distill.schemas import ColdStartState

    state = ColdStartState(
        feedback_count=3,
        state="warming",
        ratings_remaining_to_personalize=2,
        personalization_enabled_at=None,
    )
    assert state.feedback_count == 3
    assert state.state == "warming"
    assert state.ratings_remaining_to_personalize == 2
    assert state.personalization_enabled_at is None


# ---------------------------------------------------------------------------
# 9. Pipeline 集成 - default_post_step_hooks 不变
# ---------------------------------------------------------------------------
def test_default_post_step_hooks_cp561_unchanged():
    """CP5.6.1：default_post_step_hooks 仍 1 个（CP3.6.4 baseline）。"""
    from distill.hooks_impl import StageCacheHook, default_post_step_hooks

    hooks = default_post_step_hooks()
    assert len(hooks) == 1
    assert isinstance(hooks[0], StageCacheHook)
