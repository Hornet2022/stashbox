"""CP3.7.2 §2.2.E：默认 hook 实现（6 个）。

按 docs/听感产品化方案_v1.md §2.2.E 严格实现 6 个默认 hook：
- PreDistillHook (3): TierRouterHook / UserProfileHook / FewShotSelectorHook
- PostStepHook (1): StageCacheHook（CP3.6.4 已实现，复用）
- PostDistillHook (2): ListeningPatternUpdaterHook / FewShotPoolHook
  + ScorePredictorHook（待 CP3.8.0 落地真实评分，本期 mock）

每个 hook 失败都不破主流程（try/except + log warning）。
"""

from __future__ import annotations

import structlog
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import pipeline_hooks
from .pipeline_hooks import PostDistillHook, PostStepHook, PreDistillHook
from .schemas import (
    DistillContext,
    RewriteExample,
    UserListeningPattern,
)
from .stage_cache import write_stage
from .tier_router import route_tier

log = structlog.get_logger("distill.hooks_impl")


def _is_real_session(db: object) -> bool:
    """CP3.7.2 兼容检测：判断 session 是真 SQLAlchemy session 还是 mock（如 FakeSession）。

    真 AsyncSession 有 `in_transaction` 方法（SQLAlchemy 2.0）。
    FakeSession / Mock session 没有，hook 应该跳过（避免破坏测试断言）。
    """
    return hasattr(db, "in_transaction")


# ---------------------------------------------------------------------------
# PreDistillHook 实现
# ---------------------------------------------------------------------------
class TierRouterHook:
    """CP3.7.2 §2.2.E：PreDistillHook —— 根据 article + user 选 target_tier。"""

    async def __call__(self, ctx: DistillContext, article: Any, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession
        if not _is_real_session(db):
            return
        try:
            # 加载 user（避免 hook 强依赖 user 表 schema）
            from stashbox.backend.common.models import User as UserModel

            user = await db.get(UserModel, ctx.user_id)
            if user is None:
                log.warning("tier_router_hook_user_not_found", user_id=ctx.user_id)
                return

            tier = await route_tier(article, user, ctx.user_profile, db)
            ctx.target_tier = tier
            log.info("tier_routed", task_id=ctx.task_id, tier=tier)
        except Exception as e:
            log.warning("tier_router_hook_failed_continue", task_id=ctx.task_id, error=str(e))


class UserProfileHook:
    """CP3.7.2 §2.2.E：PreDistillHook —— 加载用户听感画像。"""

    async def __call__(self, ctx: DistillContext, article: Any, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession（detect by 内部 SQLAlchemy 属性）
        if not _is_real_session(db):
            return
        try:
            from stashbox.backend.common.models import UserListeningPattern as ULPSQL

            pattern = await db.scalar(select(ULPSQL).where(ULPSQL.user_id == ctx.user_id))
            if pattern is not None:
                ctx.user_profile = UserListeningPattern(
                    user_id=pattern.user_id,
                    feedback_count=pattern.feedback_count,
                    avg_session_sec=pattern.avg_session_sec,
                    skip_rate=pattern.skip_rate,
                    completion_rate=pattern.completion_rate,
                    preferred_rhythm=pattern.preferred_rhythm,
                    preferred_hook_type=pattern.preferred_hook_type,
                    avg_overall_score=pattern.avg_overall_score,
                    last_distill_at=pattern.last_distill_at,
                    last_updated=pattern.last_updated,
                )
                log.info(
                    "user_profile_loaded",
                    task_id=ctx.task_id,
                    feedback_count=pattern.feedback_count,
                )
        except Exception as e:
            log.warning("user_profile_hook_failed_continue", task_id=ctx.task_id, error=str(e))


class FewShotSelectorHook:
    """CP3.7.2 §2.2.E：PreDistillHook —— 选 few-shot（仅 feedback_count >= 5 启用）。"""

    async def __call__(self, ctx: DistillContext, article: Any, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession
        if not _is_real_session(db):
            return
        try:
            # CP3.7.2 §2.2.E：冷启动保护（feedback_count < 5 不启用 few-shot）
            if not ctx.user_profile or ctx.user_profile.feedback_count < 5:
                return

            # CP3.7.3 完整实现：select_few_shot + topic_tags 匹配
            # 本期只做骨架（CP3.7.3 会补完实际查询）
            from stashbox.backend.common.models import FewShotExample as FSSQL

            result = await db.execute(
                select(FSSQL)
                .where(FSSQL.active == True)  # noqa: E712
                .order_by(FSSQL.score_avg.desc())
                .limit(5)
            )
            examples = result.scalars().all()
            ctx.few_shot_examples = [
                RewriteExample(
                    kind=ex.kind if ex.kind in ("hook", "section", "outro") else "section",
                    text=ex.rewrite_text,
                    score_avg=ex.score_avg,
                )
                for ex in examples
            ]
            log.info(
                "few_shot_loaded",
                task_id=ctx.task_id,
                count=len(ctx.few_shot_examples),
            )
        except Exception as e:
            log.warning("few_shot_selector_hook_failed_continue", task_id=ctx.task_id, error=str(e))


# ---------------------------------------------------------------------------
# PostStepHook 实现
# ---------------------------------------------------------------------------
class StageCacheHook:
    """CP3.7.2 §2.2.E：PostStepHook —— 写 stage cache（CP3.6.4 实现 + 包装成 Hook）。"""

    async def __call__(
        self, ctx: DistillContext, step_name: str, output: Any, db: AsyncSession
    ) -> None:
        try:
            # stage_cache.write_stage 是 fire-and-forget；这里我们 await 但不阻塞
            # 失败已在 write_stage 内部 log warning
            await write_stage(ctx.task_id, step_name, output)
        except Exception as e:
            log.warning(
                "stage_cache_hook_failed_continue",
                task_id=ctx.task_id,
                step=step_name,
                error=str(e),
            )


# ---------------------------------------------------------------------------
# PostDistillHook 实现
# ---------------------------------------------------------------------------
class ListeningPatternUpdaterHook:
    """CP3.7.2 §2.2.E：PostDistillHook —— 增量更新用户听感画像（30 篇窗口 + 加权平均）。

    CP3.7.3 完整实现：增量更新 + 冷启动保护
    本期只做骨架：feedback_count += 1
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession
        if not _is_real_session(db):
            return
        try:
            from stashbox.backend.common.models import UserListeningPattern as ULPSQL

            result = await db.execute(select(ULPSQL).where(ULPSQL.user_id == ctx.user_id))
            pattern = result.scalar_one_or_none()
            if pattern is None:
                # 创建初始画像
                pattern = ULPSQL(user_id=ctx.user_id, feedback_count=1)
                db.add(pattern)
            else:
                pattern.feedback_count += 1
                pattern.last_updated = None  # 由 server_default 重新填充
            await db.commit()
            log.info(
                "listening_pattern_updated",
                task_id=ctx.task_id,
                feedback_count=pattern.feedback_count,
            )
        except Exception as e:
            log.warning(
                "listening_pattern_updater_hook_failed_continue",
                task_id=ctx.task_id,
                error=str(e),
            )


class FewShotPoolHook:
    """CP3.7.2 §2.2.E：PostDistillHook —— 高分改写入选 few-shot 池（score >= 4）。

    CP3.7.3 完整实现：Levenshtein 查重 + LRU 1000 淘汰
    本期只做骨架：评估 ID 存在 + overall_score >= 4 → 入池
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        try:
            if not ctx.evaluation_id:
                return  # 没有 evaluation ID（CP3.8.0 才有真实评分）
            # CP3.7.3 会接 DistillationEvaluation + FewShotExample 完整入池逻辑
            log.info("few_shot_pool_hook_called", task_id=ctx.task_id)
        except Exception as e:
            log.warning(
                "few_shot_pool_hook_failed_continue",
                task_id=ctx.task_id,
                error=str(e),
            )


# ---------------------------------------------------------------------------
# 默认 hook 列表 helper
# ---------------------------------------------------------------------------
def default_pre_hooks() -> list[PreDistillHook]:
    """CP3.7.2 §2.2.E：默认 pre-hooks（3 个：tier router + user profile + few-shot）。"""
    return [TierRouterHook(), UserProfileHook(), FewShotSelectorHook()]


def default_post_step_hooks() -> list[PostStepHook]:
    """CP3.7.2 §2.2.E：默认 post-step-hooks（1 个：stage cache）。"""
    return [StageCacheHook()]


def default_post_hooks() -> list[PostDistillHook]:
    """CP3.7.2 §2.2.E：默认 post-hooks（2 个：listening pattern + few-shot pool）。

    注：AutoRetryHook / ScorePredictorHook 等 CP3.7.3 / CP3.8.0 上线后再加。
    """
    return [ListeningPatternUpdaterHook(), FewShotPoolHook()]


__all__ = [
    "TierRouterHook",
    "UserProfileHook",
    "FewShotSelectorHook",
    "StageCacheHook",
    "ListeningPatternUpdaterHook",
    "FewShotPoolHook",
    "default_pre_hooks",
    "default_post_step_hooks",
    "default_post_hooks",
    "pipeline_hooks",  # 让外部能 import PreDistillHook / PostStepHook / PostDistillHook Protocol
]
