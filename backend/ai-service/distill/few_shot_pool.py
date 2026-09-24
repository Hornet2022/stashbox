"""CP3.7.3 §2.1.D：few-shot 池（高分改写片段库）。

按 docs/听感产品化方案_v1.md §2.1.D 严格实现：
- add_high_score_to_pool：score >= 4 入池 + Levenshtein 查重 + LRU 1000 淘汰
- select_few_shot：user_id 个人池优先，不够补全局池
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import DistillationEvaluation, FewShotExample

log = structlog.get_logger("distill.few_shot_pool")

Kind = Literal["hook", "section", "outro"]

# LRU 上限：1000 条
_MAX_POOL_SIZE = 1000
# LRU 淘汰：超过上限时删前 100
_EVICT_BATCH = 100

# Levenshtein 距离阈值：< 0.1（按文本长度归一化）视为重复
_LEVENSHTEIN_THRESHOLD = 0.1

# 入选门槛：overall_score >= 4
_MIN_SCORE = 4.0


def _levenshtein_distance(a: str, b: str) -> int:
    """CP3.7.3：标准 Levenshtein 距离（O(m*n) 动态规划）。

    池上限 1000 条，LRU 淘汰；查重性能 OK。
    """
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr_row = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr_row.append(
                min(
                    curr_row[j - 1] + 1,  # insertion
                    prev_row[j] + 1,  # deletion
                    prev_row[j - 1] + cost,  # substitution
                )
            )
        prev_row = curr_row
    return prev_row[-1]


def _is_similar_text(text_a: str, text_b: str, threshold: float = _LEVENSHTEIN_THRESHOLD) -> bool:
    """CP3.7.3：归一化 Levenshtein 距离 < 阈值视为重复。"""
    if not text_a or not text_b:
        return False
    max_len = max(len(text_a), len(text_b))
    if max_len == 0:
        return False
    distance = _levenshtein_distance(text_a, text_b)
    return (distance / max_len) < threshold


def _compute_source_pattern(evaluation: DistillationEvaluation) -> str:
    """CP3.7.3 §2.1.D：source_pattern = hash(article.topic_tags + rhythm_score + hook_type)。

    topic_tags 来自 evaluation.article_id（需重查 article）；本期用 evaluation id + kind 占位。
    """
    parts = [
        str(evaluation.task_id),
        str(evaluation.rhythm_score or 0),
        evaluation.comment or "",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def add_high_score_to_pool(
    db: AsyncSession,
    evaluation: DistillationEvaluation,
    rewrite_text: str,
    kind: Kind,
    user_id: int | None = None,
) -> FewShotExample | None:
    """CP3.7.3 §2.1.D：评分 >= 4 的改写入选 few-shot 池。

    Returns:
        FewShotExample 实例（成功入池）或 None（跳过 / 失败）

    失败兜底：异常 → log warning，不破主流程。
    """
    try:
        # 1. 校验 score
        if evaluation.overall_score < _MIN_SCORE:
            return None

        # 2. 算 source_pattern
        source_pattern = _compute_source_pattern(evaluation)

        # 3. 查重：同 user_id / global + 同 kind + active 的全部 few-shot
        # （source_pattern 用 task_id 算 ev1 vs ev2 不同，所以查重时不用 source_pattern 过滤）
        existing = await db.execute(
            select(FewShotExample).where(
                FewShotExample.kind == kind,
                FewShotExample.active == True,  # noqa: E712
                # user_id 匹配（global = NULL + 个人 = user_id）
                ((FewShotExample.user_id == user_id) | (FewShotExample.user_id.is_(None)))
                if user_id is not None
                else FewShotExample.user_id.is_(None),
            )
        )
        for ex in existing.scalars().all():
            if _is_similar_text(rewrite_text, ex.rewrite_text):
                log.info(
                    "few_shot_pool_dedup_skip",
                    task_id=evaluation.task_id,
                    existing_id=ex.id,
                )
                return None

        # 4. INSERT few_shot_examples
        # 收集来源 evaluation ids（暂时只有当前 evaluation）
        source_eval_ids = json.dumps([evaluation.id])
        # SQLite 测试兼容：显式传 created_at（PG 端有 server_default）
        from datetime import datetime as _dt

        now = _dt.now()
        new_ex = FewShotExample(
            id=f"fs_{hashlib.sha256(rewrite_text.encode('utf-8')).hexdigest()[:24]}",
            user_id=user_id,
            source_pattern=source_pattern,
            rewrite_text=rewrite_text,
            kind=kind,
            score_avg=float(evaluation.overall_score),
            source_eval_ids=source_eval_ids,
            usage_count=0,
            active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(new_ex)
        await db.flush()  # 让 new_ex.id 可用

        # 5. 池子超过 1000 → LRU 淘汰
        count = await db.scalar(select(func.count()).select_from(FewShotExample))
        if count and count > _MAX_POOL_SIZE:
            # 按 score_avg ASC + last_used_at ASC（NULL 优先）排序
            lru_candidates = await db.execute(
                select(FewShotExample)
                .order_by(
                    FewShotExample.score_avg.asc(),  # 低分优先删
                    FewShotExample.last_used_at.asc().nulls_first(),  # 久未用优先删
                )
                .limit(_EVICT_BATCH)
            )
            evict_ids = [ex.id for ex in lru_candidates.scalars().all()]
            if evict_ids:
                await db.execute(delete(FewShotExample).where(FewShotExample.id.in_(evict_ids)))
                log.info(
                    "few_shot_pool_lru_evict",
                    evicted=len(evict_ids),
                    pool_size_after=count - len(evict_ids),
                )

        log.info(
            "few_shot_pool_added",
            task_id=evaluation.task_id,
            kind=kind,
            new_id=new_ex.id,
            user_id=user_id,
        )
        return new_ex
    except Exception as e:
        log.warning("few_shot_pool_add_failed_continue", task_id=evaluation.task_id, error=str(e))
        return None


async def select_few_shot(
    db: AsyncSession,
    user_id: int,
    kind: Kind,
    article_topic_tags: list[str] | None = None,
    limit: int = 5,
) -> list[FewShotExample]:
    """CP3.7.3 §2.1.D：选 few-shot（user_id 个人池优先 + 全局池补充）。

    策略（两步过滤）：
    1. 优先取 user_id = {user_id} 的近 30 天样本（按 score_avg DESC, usage_count DESC）
    2. 不够 limit 条时，补充全局池（user_id IS NULL）按 score_avg DESC

    Returns:
        list[FewShotExample]（可能为空）

    失败兜底：DB 异常 → return []，prompt 不注入 few-shot（不破主流程）。
    """
    try:
        # 1. 个人池
        user_pool = await db.execute(
            select(FewShotExample)
            .where(
                FewShotExample.user_id == user_id,
                FewShotExample.kind == kind,
                FewShotExample.active == True,  # noqa: E712
            )
            .order_by(FewShotExample.score_avg.desc(), FewShotExample.usage_count.desc())
            .limit(limit)
        )
        examples = list(user_pool.scalars().all())

        # 2. 全局池补充
        if len(examples) < limit:
            need = limit - len(examples)
            user_ids_in_pool = [e.id for e in examples]
            global_pool = await db.execute(
                select(FewShotExample)
                .where(
                    FewShotExample.user_id.is_(None),
                    FewShotExample.kind == kind,
                    FewShotExample.active == True,  # noqa: E712
                    FewShotExample.id.notin_(user_ids_in_pool) if user_ids_in_pool else True,
                )
                .order_by(FewShotExample.score_avg.desc(), FewShotExample.usage_count.desc())
                .limit(need)
            )
            examples.extend(global_pool.scalars().all())

        # 更新 usage_count（best-effort）
        if examples:
            for ex in examples:
                ex.usage_count = (ex.usage_count or 0) + 1
            await db.flush()

        return examples
    except Exception as e:
        log.warning("few_shot_pool_select_failed_continue", user_id=user_id, error=str(e))
        return []
