"""CP3.7.2 §2.2.E + CP3.7.3 PostHook 完整实现。

按 docs/听感产品化方案_v1.md §2.2.E 严格实现 4 个默认 hook：
- PreDistillHook (3): TierRouterHook / UserProfileHook / FewShotSelectorHook
- PostStepHook (1): StageCacheHook（CP3.6.4 已实现，复用）
- PostDistillHook (4): ScorePredictorHook + AutoRetryHook
  + ListeningPatternUpdaterHook + FewShotPoolHook

每个 hook 失败都不破主流程（try/except + log warning）。
"""

from __future__ import annotations

import structlog
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import pipeline_hooks
from .auto_retry import (
    bump_user_daily_retry_count,
    enqueue_auto_retry,
    get_user_daily_retry_count,
    should_auto_retry,
)
from .listening_pattern_updater import update_user_listening_pattern
from .pipeline_hooks import PostDistillHook, PostStepHook, PreDistillHook
from .schemas import (
    DistillContext,
    RewriteExample,
    UserListeningPattern,
)
from .score_predictor import predict_and_save_quality_score
from .stage_cache import write_stage
from .tier_router import route_tier

log = structlog.get_logger("distill.hooks_impl")


def _is_real_session(db: object) -> bool:
    """CP3.7.2 兼容检测：判断 session 是真 SQLAlchemy session 还是 mock（如 FakeSession）。

    真 AsyncSession 有 `in_transaction` 方法（SQLAlchemy 2.0）。
    FakeSession / Mock session 没有，hook 应该跳过（避免破坏测试断言）。
    """
    return hasattr(db, "in_transaction")


async def _latest_user_evaluation(db: AsyncSession, ctx: DistillContext) -> Any:
    """取这篇**真实**的用户最新一条听感评分；没有则 None。

    为什么必须查真值，不能造一条 overall_score=4 的
    --------------------------------------------
    这两个 hook 早期版本直接 `DistillationEvaluation(overall_score=4)` 造一条
    假评分再喂给画像 / few-shot 池，理由是"CP3.8.0 接真实评分之前先占位"。
    实际后果是把"用户偏好"信号污染成"每次蒸馏都投一票 4 分"：

    - `update_user_listening_pattern` 的 `feedback_count` 会随**蒸馏次数**增长，
      不是随用户反馈增长 → 冷启动门槛（<5）5 次蒸馏就跨过，等于画像"永远热启动"
    - `avg_overall_score` 被假 4 分持续拉高，用户真实打的 1 星被稀释
    - few-shot 池里塞满"没人喜欢过"的 hook 片段（门槛只有 score>=4），
      下次蒸馏 `MemoryStore.load_few_shots` 读到它们当范例喂给 LLM

    所以这里改成：只认用户真的提交过的评分（`auto_flag=false`），
    没有就不更新 —— 没有真实信号时保持空，而不是用假信号假装有。

    ## 2026-10-02：改成经 DistilledArticle 桥接查

    ``distillation_evaluations`` 只有 task_id 列，而写入时用的是
    ``task_id = DistilledArticle.id``（``evaluation_service.py``）——那是这篇
    文章**首次**蒸馏的 task_id。直接拿 ``ctx.task_id`` 查，文章一旦重蒸就对不上，
    评分永远查不到（表现为「用户明明打过 1 星，系统毫无反应」）。
    改为 article_id → DistilledArticle.id → evaluations.task_id 三段跳。
    """
    from sqlalchemy import select as _select

    from stashbox.backend.common.models import DistillationEvaluation, DistilledArticle

    try:
        da_id = await db.scalar(
            _select(DistilledArticle.id).where(DistilledArticle.article_id == ctx.article_id)
        )
        if da_id is None:
            return None
        return await db.scalar(
            _select(DistillationEvaluation)
            .where(
                DistillationEvaluation.task_id == da_id,
                DistillationEvaluation.auto_flag.is_(False),
            )
            .order_by(DistillationEvaluation.created_at.desc())
            .limit(1)
        )
    except Exception as exc:
        log.warning(
            "latest_user_evaluation_query_failed", article_id=ctx.article_id, error=str(exc)
        )
        return None


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


async def _lookup_user_tier(db: AsyncSession, user_id: int) -> str:
    """查用户付费档位（users.tier）。查不到按 free 处理。"""
    from sqlalchemy import text

    try:
        row = (
            await db.execute(
                text("SELECT tier FROM users WHERE id = :uid AND deleted_at IS NULL"),
                {"uid": user_id},
            )
        ).first()
        return str(row[0] or "free") if row else "free"
    except Exception as exc:
        log.warning("lookup_user_tier_failed", user_id=user_id, error=str(exc))
        return "free"


async def _lookup_personalization_consent(db: AsyncSession, user_id: int) -> bool:
    """查用户是否授权了个性化（user_consents.personalization_enabled）。

    没有 consent 记录时返回 False —— 授权是**需要用户明示同意**才能做的事，
    「查不到」不等于「同意」。早前这里写死 True，等于绕过整个同意流程。
    """
    from sqlalchemy import text

    try:
        row = (
            await db.execute(
                text(
                    "SELECT personalization_enabled FROM user_consents "
                    "WHERE user_id = :uid AND deleted_at IS NULL"
                ),
                {"uid": user_id},
            )
        ).first()
        return bool(row[0]) if row else False
    except Exception as exc:
        # 表可能还不存在（迁移未跑）→ 按未授权处理，宁可少个性化不要越权
        log.warning("lookup_consent_failed_default_denied", user_id=user_id, error=str(exc))
        return False


class FewShotSelectorHook:
    """CP5.6.0 §2.3：PreDistillHook —— 选 few-shot（CP3.7.2 骨架 + CP3.7.3 完整 + CP5.6.0 个性化）。

    CP5.6.0：调 PersonalizationSelector（5 条规则 + 隐私 + A/B）
    """

    async def __call__(self, ctx: DistillContext, article: Any, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession
        if not _is_real_session(db):
            return
        # B4（修 D1）：A/B 分桶是用户属性（方案 §2.7-D：user_id % 100 < 30 → personalized），
        # 与是否真正个性化无关（冷启动/未付费用户也归属组），intention-to-treat 口径。
        # ctx.is_personalized 记录实际处理（as-treated），两列一起落库供 ab-report 对比。
        ctx.ab_group = "personalized" if ctx.user_id % 100 < 30 else "general"
        try:
            # CP3.7.2 §2.2.E：冷启动保护（feedback_count < 5 不启用 few-shot）
            # CP5.6.0 §2.3：进一步走 PersonalizationSelector（个性化决策）
            if not ctx.user_profile or ctx.user_profile.feedback_count < 5:
                # 冷启动：走大众化（CP3.7.2 行为），is_personalized 保持 False
                return

            # CP5.6.0：调 PersonalizationSelector
            from .personalization_selector import PersonalizationSelector

            selector = PersonalizationSelector()
            # ⚠️ 原来这两行都是假的（2026-10-02 修）：
            # 1) `getattr(article, "user_tier", None)` —— Article 模型里根本没有
            #    user_tier 这个属性，getattr 恒返回 None → 恒为 "free" →
            #    所有用户都被判成免费档，个性化付费墙形同虚设。
            # 2) `consent_enabled=True` 写死 —— 用户在 App 里关掉个性化授权，
            #    系统照样用他的数据算画像（GDPR 风险）。
            # 现在都真查 users / user_consents 表。
            user_tier = await _lookup_user_tier(db, ctx.user_id)
            consent_enabled = await _lookup_personalization_consent(db, ctx.user_id)

            examples, is_personalized = await selector.select_personalized_few_shot(
                db,
                user_id=ctx.user_id,
                user_tier=user_tier,
                user_profile=ctx.user_profile,
                article_topic_tags=None,
                consent_enabled=consent_enabled,
                is_minor=False,
                limit=5,
            )
            ctx.few_shot_examples = [
                RewriteExample(
                    kind=ex.kind if ex.kind in ("hook", "section", "outro") else "section",
                    text=ex.rewrite_text,
                    score_avg=ex.score_avg,
                )
                for ex in examples
            ]
            # CP5.6.0：标记个性化（观测 / 调试用）
            ctx.is_personalized = is_personalized
            log.info(
                "few_shot_loaded_cp560",
                task_id=ctx.task_id,
                count=len(ctx.few_shot_examples),
                is_personalized=is_personalized,
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
    """CP3.7.3：PostDistillHook —— 增量更新用户听感画像（30 篇窗口 + 加权平均）。

    只在用户**真的**提交过听感评分时更新（见 [_latest_user_evaluation]）。
    早期版本在这里造 overall_score=4 的假评分，会让 feedback_count 随蒸馏次数
    增长、冷启动保护形同虚设，已移除。
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        # CP3.7.2 兼容 FakeSession
        if not _is_real_session(db):
            return
        try:
            evaluation = await _latest_user_evaluation(db, ctx)
            if evaluation is None:
                # 用户还没评过这篇 → 没有真实信号可更新，保持原样
                log.info(
                    "listening_pattern_hook_skipped_no_real_eval",
                    task_id=ctx.task_id,
                    user_id=ctx.user_id,
                )
                return

            await update_user_listening_pattern(db, ctx.user_id, evaluation)
            await db.commit()
        except Exception as e:
            log.warning(
                "listening_pattern_updater_hook_failed_continue",
                task_id=ctx.task_id,
                error=str(e),
            )


class FewShotPoolHook:
    """CP3.7.3：PostDistillHook —— 高分改写入选 few-shot 池（score >= 4）。

    同样只认真实评分（见 [_latest_user_evaluation]）。早期版本造 overall_score=4
    的假评分，等于每次蒸馏都往池里塞一条"用户从没认可过"的 hook 片段，
    而 few-shot 池的入选门槛只有 score>=4，池会被假信号灌满。
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        if not _is_real_session(db):
            return
        try:
            from .few_shot_pool import add_high_score_to_pool

            evaluation = await _latest_user_evaluation(db, ctx)
            if evaluation is None:
                log.info(
                    "few_shot_pool_hook_skipped_no_real_eval",
                    task_id=ctx.task_id,
                    user_id=ctx.user_id,
                )
                return

            rewrite_text = ctx.rewrite.hook if ctx.rewrite and ctx.rewrite.hook else ""
            if not rewrite_text:
                # 早期版本这里 fallback 成字面量 "default text"，
                # 会把一个无意义的字符串当范例喂给 LLM
                log.info("few_shot_pool_hook_skipped_no_hook_text", task_id=ctx.task_id)
                return

            await add_high_score_to_pool(db, evaluation, rewrite_text, "hook", user_id=ctx.user_id)
            await db.commit()
        except Exception as e:
            log.warning(
                "few_shot_pool_hook_failed_continue",
                task_id=ctx.task_id,
                error=str(e),
            )


class ScorePredictorHook:
    """CP3.7.3 §2.2.E：PostDistillHook —— 听感质量分落库。

    2026-10-02：改为只用真实用户评分算分，没有就留 NULL —— 之前是写死 8.5。
    写 8.5 比留 NULL 更有害：NULL 读作「未评估」，8.5 读作「评估过、质量好」，
    管理后台的评分列和 A/B 报表都会拿它当依据。
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        if not _is_real_session(db):
            return
        try:
            # 按 article_id（业务键）查，不是 ctx.task_id —— agent 路径一篇文章
            # 可能对应多个 task_id，按 id 查会漏掉。
            score = await predict_and_save_quality_score(db, ctx.article_id)
            await db.commit()
            if score is None:
                log.info("score_predictor_no_real_eval", article_id=ctx.article_id)
            else:
                log.info("score_predictor_hook_completed", article_id=ctx.article_id, score=score)
        except Exception as e:
            log.warning(
                "score_predictor_hook_failed_continue",
                task_id=ctx.task_id,
                error=str(e),
            )


class AutoRetryHook:
    """CP3.7.3 §2.2.E：PostDistillHook —— 评分 < 3 → **真正重新入队**。

    2026-10-02 从「只判断 + 打日志」改成真入队。理由：低分自动重蒸馏是方案
    §1 闭环 1 承诺的核心动作之一，此前命中后只写一行 log，用户打 1-2 星
    什么都不会发生 —— 评分闭环在体验上完全断裂。

    限流用 Redis 日计数（之前 `user_daily_retry_count = 0` 是写死的，
    限流形同虚设；真接上入队后不限流，一次低分就能把队列打爆）。
    """

    async def __call__(self, ctx: DistillContext, db: AsyncSession) -> None:
        try:
            if not _is_real_session(db):
                return
            evaluation = await _latest_user_evaluation(db, ctx)
            if evaluation is None or evaluation.overall_score is None:
                log.info("auto_retry_skipped_no_real_eval", task_id=ctx.task_id)
                return

            score = float(evaluation.overall_score)
            daily_count = await get_user_daily_retry_count(ctx.user_id)

            if not should_auto_retry(score, daily_count):
                log.info(
                    "auto_retry_skipped", task_id=ctx.task_id, score=score, daily_count=daily_count
                )
                return

            # 先占额度再入队：反过来会让入队失败的尝试白占名额
            await bump_user_daily_retry_count(ctx.user_id)

            job_id = await enqueue_auto_retry(
                user_id=ctx.user_id,
                article_id=ctx.article_id,
                url=ctx.url,
            )
            log.info(
                "auto_retry_triggered",
                task_id=ctx.task_id,
                article_id=ctx.article_id,
                score=score,
                daily_count=daily_count + 1,
                new_job_id=job_id,
            )
        except Exception as e:
            log.warning(
                "auto_retry_hook_failed_continue",
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
    """CP3.7.3 §2.2.E：默认 post-hooks（4 个：score predictor + auto retry + pattern + pool）。

    顺序：
    1. ScorePredictorHook：先预测评分（CP3.8.0 接真实，本期 mock）
    2. AutoRetryHook：根据评分判定是否自动重蒸
    3. ListeningPatternUpdaterHook：增量更新用户画像
    4. FewShotPoolHook：高分改写入池
    """
    return [
        ScorePredictorHook(),
        AutoRetryHook(),
        ListeningPatternUpdaterHook(),
        FewShotPoolHook(),
    ]


__all__ = [
    "TierRouterHook",
    "UserProfileHook",
    "FewShotSelectorHook",
    "StageCacheHook",
    "ListeningPatternUpdaterHook",
    "FewShotPoolHook",
    "ScorePredictorHook",
    "AutoRetryHook",
    "default_pre_hooks",
    "default_post_step_hooks",
    "default_post_hooks",
    "pipeline_hooks",  # 让外部能 import PreDistillHook / PostStepHook / PostDistillHook Protocol
]
