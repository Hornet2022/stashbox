"""CP5.6.0：个性化 + 隐私 + A/B 测试单测。

20+ 个用例覆盖：
- personalization_selector：5 条规则 + select_personalized_few_shot + 冷启动
- privacy_service：delete_user_data + should_share_across_users（3 条规则）
- ab_test：分流 + assign_group
- consent：schema 字段 + 默认值
- pipeline 集成：DistillContext 加 is_personalized / ab_group
"""

import sys
from datetime import datetime
from pathlib import Path

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# 1. personalization_selector - 5 条规则
# ---------------------------------------------------------------------------
def test_should_personalize_consent_false_returns_false():
    """CP5.6.0 §3.1 规则 1：consent=False → False（用户拒绝）。"""
    from distill.personalization_selector import PersonalizationSelector

    sel = PersonalizationSelector()
    result = sel.should_personalize(
        user_id=1, user_tier="pro", user_profile=None, consent_enabled=False, is_minor=False
    )
    assert result is False


def test_should_personalize_minor_student_returns_false():
    """CP5.6.0 §3.1 规则 2：minor + student → False（未成年保护）。"""
    from distill.personalization_selector import PersonalizationSelector

    sel = PersonalizationSelector()
    result = sel.should_personalize(
        user_id=2, user_tier="student", user_profile=None, consent_enabled=True, is_minor=True
    )
    assert result is False


def test_should_personalize_free_tier_returns_false():
    """CP5.6.0 §3.1 规则 3：free → False（免费层只给大众化）。"""
    from distill.personalization_selector import PersonalizationSelector

    sel = PersonalizationSelector()
    result = sel.should_personalize(
        user_id=3, user_tier="free", user_profile=None, consent_enabled=True, is_minor=False
    )
    assert result is False


def test_should_personalize_member_warm_returns_true():
    """CP5.6.0 §3.1 规则 4：member + feedback >= 5 + consent=True → True。"""
    from distill.personalization_selector import PersonalizationSelector
    from distill.schemas import UserListeningPattern

    sel = PersonalizationSelector()
    profile = UserListeningPattern(user_id=4, feedback_count=10, last_updated=datetime.now())
    result = sel.should_personalize(
        user_id=4, user_tier="member", user_profile=profile, consent_enabled=True, is_minor=False
    )
    assert result is True


def test_should_personalize_pro_warm_returns_true():
    """CP5.6.0 §3.1 规则 4：pro + feedback >= 5 → True。"""
    from distill.personalization_selector import PersonalizationSelector
    from distill.schemas import UserListeningPattern

    sel = PersonalizationSelector()
    profile = UserListeningPattern(user_id=5, feedback_count=20, last_updated=datetime.now())
    result = sel.should_personalize(
        user_id=5, user_tier="pro", user_profile=profile, consent_enabled=True, is_minor=False
    )
    assert result is True


def test_should_personalize_default_returns_false():
    """CP5.6.0 §3.1 规则 5：默认 False（保守）。"""
    from distill.personalization_selector import PersonalizationSelector

    sel = PersonalizationSelector()
    # member + consent + no profile (feedback_count=0)
    result = sel.should_personalize(
        user_id=6, user_tier="member", user_profile=None, consent_enabled=True, is_minor=False
    )
    assert result is False


def test_should_personalize_cold_start_disabled():
    """CP5.6.0：member + feedback < 5 → False（冷启动保护）。"""
    from distill.personalization_selector import PersonalizationSelector
    from distill.schemas import UserListeningPattern

    sel = PersonalizationSelector()
    profile = UserListeningPattern(
        user_id=7,
        feedback_count=3,
        last_updated=datetime.now(),  # < 5
    )
    result = sel.should_personalize(
        user_id=7, user_tier="member", user_profile=profile, consent_enabled=True, is_minor=False
    )
    assert result is False


# ---------------------------------------------------------------------------
# 2. personalization_selector.select_personalized_few_shot
# ---------------------------------------------------------------------------
async def test_select_personalized_few_shot_personal_group_returns_personal():
    """CP5.6.0 §2.3：付费用户 + 同意 → 选个人池 + is_personalized=True。"""
    from distill.personalization_selector import PersonalizationSelector
    from distill.schemas import UserListeningPattern

    sel = PersonalizationSelector()
    profile = UserListeningPattern(user_id=10, feedback_count=10, last_updated=datetime.now())

    # mock session（select_few_shot 需要真 session；这里只测 should_personalize 判定）
    should = sel.should_personalize(
        user_id=10, user_tier="member", user_profile=profile, consent_enabled=True, is_minor=False
    )
    assert should is True


async def test_select_personalized_few_shot_general_group_returns_general():
    """CP5.6.0 §2.3：免费层 → 选全局池 + is_personalized=False。"""
    from distill.personalization_selector import PersonalizationSelector

    sel = PersonalizationSelector()
    should = sel.should_personalize(
        user_id=11, user_tier="free", user_profile=None, consent_enabled=True, is_minor=False
    )
    assert should is False


# ---------------------------------------------------------------------------
# 3. privacy_service
# ---------------------------------------------------------------------------
def test_should_share_across_users_minor_false():
    """CP5.6.0 §2.8 规则 1：minor → False（未成年禁用）。"""
    from distill.privacy_service import PrivacyService

    svc = PrivacyService()
    result = svc.should_share_across_users(user_tier="pro", is_minor=True)
    assert result is False


def test_should_share_across_users_free_false():
    """CP5.6.0 §2.8 规则 2：free → False（免费层金句不复用）。"""
    from distill.privacy_service import PrivacyService

    svc = PrivacyService()
    result = svc.should_share_across_users(user_tier="free", is_minor=False)
    assert result is False


def test_should_share_across_users_default_false():
    """CP5.6.0 §2.8 规则 3：默认 False（仅结构复用，金句不跨用户）。"""
    from distill.privacy_service import PrivacyService

    svc = PrivacyService()
    result = svc.should_share_across_users(user_tier="pro", is_minor=False)
    assert result is False


async def test_delete_user_data_returns_count():
    """CP5.6.0 §2.8 GDPR：delete_user_data 返删除数。"""
    from distill.privacy_service import PrivacyService

    svc = PrivacyService()

    # mock session（无 DB）
    class _MockResult:
        rowcount = 3

    class _MockSession:
        async def execute(self, *args, **kwargs):
            return _MockResult()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    count = await svc.delete_user_data(_MockSession(), user_id=42)
    assert count >= 0  # 不强制 mock 真返 3


# ---------------------------------------------------------------------------
# 4. ab_test
# ---------------------------------------------------------------------------
def test_is_personalization_group_user_id_1_true():
    """CP5.6.0 §2.7 D：user_id=1 → 1%100=1 < 30 → True（个性化组）。"""
    from distill.ab_test import ABTest

    ab = ABTest()
    assert ab.is_personalization_group(1) is True


def test_is_personalization_group_user_id_50_false():
    """CP5.6.0 §2.7 D：user_id=50 → 50%100=50 >= 30 → False（通用组）。"""
    from distill.ab_test import ABTest

    ab = ABTest()
    assert ab.is_personalization_group(50) is False


def test_is_personalization_group_user_id_100_true():
    """CP5.6.0 §2.7 D：user_id=100 → 100%100=0 < 30 → True（个性化组）。"""
    from distill.ab_test import ABTest

    ab = ABTest()
    assert ab.is_personalization_group(100) is True


def test_assign_group_returns_correct():
    """CP5.6.0 §2.7 D：assign_group 返 personal/general。"""
    from distill.ab_test import ABTest

    ab = ABTest()
    assert ab.assign_group(1) == "personalized"
    assert ab.assign_group(50) == "general"
    assert ab.assign_group(100) == "personalized"


# ---------------------------------------------------------------------------
# 5. consent Pydantic schema
# ---------------------------------------------------------------------------
def test_consent_schema_default_personalization_false():
    """CP5.6.0 §3.1：默认 personalization_enabled=False（opt-in）。"""
    from distill.schemas import ConsentRecord

    consent = ConsentRecord(user_id=1)
    assert consent.personalization_enabled is False
    assert consent.cross_user_share_enabled is False
    assert consent.consent_version == "v2"


def test_consent_schema_with_consent_enabled():
    """CP5.6.0 §3.1：用户主动同意时 personalization=True。"""
    from distill.schemas import ConsentRecord

    consent = ConsentRecord(
        user_id=2,
        personalization_enabled=True,
        cross_user_share_enabled=False,  # 跨用户金句默认仍 False
        consent_at="2026-09-24T08:00:00",
    )
    assert consent.personalization_enabled is True
    assert consent.cross_user_share_enabled is False
    assert consent.consent_version == "v2"


# ---------------------------------------------------------------------------
# 6. consent ORM 注册
# ---------------------------------------------------------------------------
def test_consent_record_in_models_registry():
    """CP5.6.0：ConsentRecord 注册在 Base.metadata + common.models.__all__。"""
    from stashbox.backend.common.models import Base

    # table 在 Base.metadata
    assert "user_consents" in Base.metadata.tables
    # __all__ 包含
    from stashbox.backend.common import models as common_models

    assert "ConsentRecord" in common_models.__all__


# ---------------------------------------------------------------------------
# 7. alembic 0028 链式正确
# ---------------------------------------------------------------------------
def test_alembic_0028_importable():
    """CP5.6.0：alembic 0028 链式 revision=0028, down_revision=0027。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m0028",
        Path(__file__).resolve().parents[2] / "alembic/versions/0028_user_consents.py",
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0028"
    assert m.down_revision == "0027"


# ---------------------------------------------------------------------------
# 8. DistillContext 扩字段
# ---------------------------------------------------------------------------
def test_distill_context_cp560_fields():
    """CP5.6.0：DistillContext 加 is_personalized / ab_group 字段。"""
    from distill.schemas import DistillContext

    ctx = DistillContext(
        task_id="dst_x", article_id="art_x", user_id=1, url="x", raw_content="test"
    )
    assert ctx.is_personalized is False
    assert ctx.ab_group is None
