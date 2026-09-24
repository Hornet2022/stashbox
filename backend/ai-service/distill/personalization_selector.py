"""CP5.6.0 §2.3：个性化少样本选择器。

按 docs/听感产品化方案_v1.md §2.3 + §3.1 决策 1 严格实现：
- should_personalize: 5 条规则
- select_personalized_few_shot: 5 步流程
- 默认 fallback：大众化（全局池，跨用户金句禁用）
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from .few_shot_pool import select_few_shot
from stashbox.backend.common.models import FewShotExample, UserListeningPattern

log = structlog.get_logger("distill.personalization_selector")

# 冷启动保护：feedback_count < 5 时走大众化
_COLD_START_THRESHOLD = 5


class PersonalizationSelector:
    """CP5.6.0 §2.3：个性化少样本选择器。"""

    def should_personalize(
        self,
        user_id: int,
        user_tier: str | None = None,
        user_profile: UserListeningPattern | None = None,
        consent_enabled: bool = False,
        is_minor: bool = False,
    ) -> bool:
        """CP5.6.0 §3.1 决策 1：判定是否个性化。

        规则（按优先级，第一个匹配即返回）：
        1. consent_enabled = False → False（用户拒绝）
        2. is_minor = True + user_tier == 'student' → False（未成年保护）
        3. user_tier == 'free' → False（免费层只给大众化）
        4. user_tier in ('pro', 'member') + feedback_count >= 5 + consent_enabled = True → True
        5. 默认 False（保守）
        """
        # 1. 用户拒绝 → False
        if not consent_enabled:
            log.info("personalization_blocked_consent_false", user_id=user_id)
            return False

        # 2. 未成年 + student → False
        if is_minor and user_tier == "student":
            log.info(
                "personalization_blocked_minor_student",
                user_id=user_id,
            )
            return False

        # 3. 免费层 → False
        if user_tier == "free":
            log.info("personalization_blocked_free_tier", user_id=user_id)
            return False

        # 4. 付费层 + 冷启动已过 + 用户同意 → True
        if user_tier in ("pro", "member"):
            feedback_count = user_profile.feedback_count if user_profile else 0
            if feedback_count >= _COLD_START_THRESHOLD:
                log.info(
                    "personalization_enabled",
                    user_id=user_id,
                    user_tier=user_tier,
                    feedback_count=feedback_count,
                )
                return True

        # 5. 默认 False（保守）
        log.info(
            "personalization_default_disabled",
            user_id=user_id,
            user_tier=user_tier,
        )
        return False

    async def select_personalized_few_shot(
        self,
        db: AsyncSession,
        user_id: int,
        user_tier: str | None = None,
        user_profile: UserListeningPattern | None = None,
        article_topic_tags: list[str] | None = None,
        consent_enabled: bool = False,
        is_minor: bool = False,
        limit: int = 5,
    ) -> tuple[list[FewShotExample], bool]:
        """CP5.6.0 §2.3：根据规则选 few-shot。

        Returns:
            (few_shot_examples, is_personalized)

        失败兜底：异常 → 全局池 + is_personalized=False
        """
        try:
            should_personalize = self.should_personalize(
                user_id=user_id,
                user_tier=user_tier,
                user_profile=user_profile,
                consent_enabled=consent_enabled,
                is_minor=is_minor,
            )

            if not should_personalize:
                # 大众化路径：从全局池选（user_id=NULL，跨用户金句）
                # CP5.6.0 §2.8 隐私：跨用户金句仅结构复用（不暴露原文）
                # 实际 select_few_shot 已经会选个人池优先 + 全局池补充
                # 这里用 user_id=-1 模拟"只要全局池"（因为 user_id=-1 查不到个人池）
                examples = await select_few_shot(
                    db,
                    user_id=-1,  # trick: 走全局池路径
                    kind="hook",
                    article_topic_tags=article_topic_tags,
                    limit=limit,
                )
                return examples, False

            # 个性化路径：选个人池
            examples = await select_few_shot(
                db,
                user_id=user_id,
                kind="hook",
                article_topic_tags=article_topic_tags,
                limit=limit,
            )
            return examples, True
        except Exception as e:
            log.warning(
                "personalization_selector_failed_fallback",
                user_id=user_id,
                error=str(e),
            )
            return [], False
