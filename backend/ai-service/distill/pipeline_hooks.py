"""CP3.7.2 §2.2.B：Pipeline Hook Protocol（3 个钩子）。

按 docs/听感产品化方案_v1.md §2.2.B 严格实现：
- PreDistillHook：蒸馏启动前（tier 路由 / 用户画像 / few-shot 选）
- PostStepHook：每步完成后（stage cache / metrics / langfuse）
- PostDistillHook：蒸馏完成后（画像更新 / few-shot 入池 / 自动重蒸）
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import DistillContext


class PreDistillHook(Protocol):
    """CP3.7.2 §2.2.B：蒸馏启动前 hook。

    实现：
    1. TierRouterHook: 选择 target_tier
    2. UserProfileHook: 加载 user_profile
    3. FewShotSelectorHook: 加载 few_shot_examples
    """

    async def __call__(
        self,
        ctx: DistillContext,
        article: Any,  # stashbox.backend.common.models.Article（避免循环 import）
        db: AsyncSession,
    ) -> None: ...


class PostStepHook(Protocol):
    """CP3.7.2 §2.2.B：每步完成后 hook。

    实现：
    1. StageCacheHook: 写 Redis stage:{task_id}:{step_name}（CP3.6.4）
    2. MetricsHook: 打 Prometheus 指标
    3. LangfuseSpanHook: 关 Langfuse span
    """

    async def __call__(
        self,
        ctx: DistillContext,
        step_name: str,
        output: Any,
        db: AsyncSession,
    ) -> None: ...


class PostDistillHook(Protocol):
    """CP3.7.2 §2.2.B：蒸馏完成后 hook。

    实现：
    1. ScorePredictorHook: 听感评分预测（mock 8.5 / CP3.8.0 接真实评分）
    2. AutoRetryHook: 评分 < 3 → 重新入队（CP3.7.3 实现）
    3. ListeningPatternUpdaterHook: 更新 user_listening_patterns
    4. FewShotPoolHook: 评分 >= 4 → 入池
    """

    async def __call__(
        self,
        ctx: DistillContext,
        db: AsyncSession,
    ) -> None: ...
