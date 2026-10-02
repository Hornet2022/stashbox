"""B4 · 缺口 A4：A/B 实验报表（方案 §2.7-D 四指标）。

分组口径（intention-to-treat）：
- ab_group = personalized / general（user_id % 100 < 30 → personalized）
- NULL 组 = 0029 迁移上线前的历史数据 → 报表归入 "pre_experiment" 单独展示

四指标（按 ab_group 聚合）：
1. 复听率：同一 (user, article) 有 ≥2 次 audio_play_start 的 pair / 有播放的 pair
2. 完听率：audio_complete 事件数 / audio_play_start 事件数
3. 评分均值：distillation_evaluations.overall_score 均值
4. 跳过率：articles.skip=true 的任务数 / 该组 done 任务数

⚠️ 诚实标注：0029 上线前无分组数据，A/B 结论只能从部署日重新计 2 周。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import (
    Article,
    DistillationEvaluation,
    DistilledArticle,
    Feedback,
)

log = structlog.get_logger("distill.ab_report")

_PLAY = "audio_play_start"  # common/events.py AUDIO_PLAY_START
_COMPLETE = "audio_complete"  # common/events.py AUDIO_COMPLETE

_PRE_GROUP = "pre_experiment"
_GROUP_ORDER = ("personalized", "general", _PRE_GROUP)


def _group_key(ab_group: str | None) -> str:
    """NULL / 未知值 → pre_experiment（0029 上线前的历史行）。"""
    return ab_group if ab_group in ("personalized", "general") else _PRE_GROUP


def _da_done(date_from: datetime | None, date_to: datetime | None):
    """所有聚合共用的蒸馏任务过滤：done + 可选 created_at 区间。"""
    conds = [DistilledArticle.status == "done"]
    if date_from is not None:
        conds.append(DistilledArticle.created_at >= date_from)
    if date_to is not None:
        conds.append(DistilledArticle.created_at < date_to)
    return conds


async def compute_ab_report(
    db: AsyncSession,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """按 ab_group 聚合 A/B 四指标，返回 {groups: [...], caveats: [...]}。

    每个指标独立 GROUP BY 查询后在 Python 侧按组合并（组数 ≤3，代价可忽略；
    避免多表 JOIN 链式聚合在 SQLite/PG 语义差异上踩坑）。
    """
    conds = _da_done(date_from, date_to)

    # 1) 组基础：任务数
    groups: dict[str, dict[str, Any]] = {}
    base_rows = (
        await db.execute(
            select(DistilledArticle.ab_group, func.count())
            .where(*conds)
            .group_by(DistilledArticle.ab_group)
        )
    ).all()
    for ab_group, cnt in base_rows:
        groups[_group_key(ab_group)] = {
            "group": _group_key(ab_group),
            "tasks": int(cnt),
            "avg_overall_score": None,
            "eval_count": 0,
            "play_count": 0,
            "complete_count": 0,
            "completion_rate": None,
            "rewatch_pairs": 0,
            "play_pairs": 0,
            "rewatch_rate": None,
            "skip_count": 0,
            "skip_rate": None,
        }

    def _merge_rows(rows, setter):
        for row in rows:
            setter(groups.get(_group_key(row[0])), row)

    # 2) 评分均值（evaluations join distilled_articles）
    eval_rows = (
        await db.execute(
            select(
                DistilledArticle.ab_group,
                func.count(DistillationEvaluation.id),
                func.avg(DistillationEvaluation.overall_score),
            )
            .join(
                DistillationEvaluation,
                DistillationEvaluation.task_id == DistilledArticle.id,
            )
            .where(*conds, DistillationEvaluation.deleted_at.is_(None))
            .group_by(DistilledArticle.ab_group)
        )
    ).all()

    def _set_eval(g, row):
        if g is None:
            return
        g["eval_count"] = int(row[1] or 0)
        g["avg_overall_score"] = round(float(row[2]), 3) if row[2] is not None else None

    _merge_rows(eval_rows, _set_eval)

    # 3) 完听率（feedback 事件计数 join distilled_articles.article_id）
    event_rows = (
        await db.execute(
            select(
                DistilledArticle.ab_group,
                func.sum(case((Feedback.type == _PLAY, 1), else_=0)),
                func.sum(case((Feedback.type == _COMPLETE, 1), else_=0)),
            )
            .join(Feedback, Feedback.article_id == DistilledArticle.article_id)
            .where(*conds, Feedback.type.in_((_PLAY, _COMPLETE)))
            .group_by(DistilledArticle.ab_group)
        )
    ).all()

    def _set_events(g, row):
        if g is None:
            return
        g["play_count"] = int(row[1] or 0)
        g["complete_count"] = int(row[2] or 0)
        if g["play_count"]:
            g["completion_rate"] = round(g["complete_count"] / g["play_count"], 4)

    _merge_rows(event_rows, _set_events)

    # 4) 复听率：per (user, article) 播放 ≥2 次的 pair / 有播放的 pair
    per_pair = (
        select(
            DistilledArticle.ab_group.label("ab_group"),
            Feedback.user_id.label("user_id"),
            Feedback.article_id.label("article_id"),
            func.count().label("plays"),
        )
        .join(Feedback, Feedback.article_id == DistilledArticle.article_id)
        .where(*conds, Feedback.type == _PLAY)
        .group_by(DistilledArticle.ab_group, Feedback.user_id, Feedback.article_id)
        .subquery()
    )
    rewatch_rows = (
        await db.execute(
            select(
                per_pair.c.ab_group,
                func.count(),
                func.sum(case((per_pair.c.plays >= 2, 1), else_=0)),
            ).group_by(per_pair.c.ab_group)
        )
    ).all()

    def _set_rewatch(g, row):
        if g is None:
            return
        g["play_pairs"] = int(row[1] or 0)
        g["rewatch_pairs"] = int(row[2] or 0)
        if g["play_pairs"]:
            g["rewatch_rate"] = round(g["rewatch_pairs"] / g["play_pairs"], 4)

    _merge_rows(rewatch_rows, _set_rewatch)

    # 5) 跳过率：articles.skip=true / 组内任务数
    skip_rows = (
        await db.execute(
            select(DistilledArticle.ab_group, func.count())
            .join(Article, Article.id == DistilledArticle.article_id)
            .where(*conds, Article.skip.is_(True))
            .group_by(DistilledArticle.ab_group)
        )
    ).all()

    def _set_skip(g, row):
        if g is None:
            return
        g["skip_count"] = int(row[1] or 0)

    _merge_rows(skip_rows, _set_skip)
    # skip 查询按"存在 skip=true 行"分组，无 skip 的组不会出现 → 统一回填比率
    for g in groups.values():
        if g["tasks"]:
            g["skip_rate"] = round(g["skip_count"] / g["tasks"], 4)

    caveats = [
        # ⚠️ 这条是「结论不可用」的硬声明，排在最前面（2026-10-02 端到端自测）。
        #
        # 前提不成立的原因：个性化分组靠 `ab_group = user_id % 100 < 30` 在**落库时**
        # 硬算（distill_task.py:546），而个性化本身却从未真正分组生效 ——
        # `agent.memory.load_few_shots` 读 few-shot 池时**不按 user_id 过滤**，
        # 所有用户拿到的是同一批样本。于是 personalized 和 general 两组的
        # 改写输入完全相同，任何指标差异都只能来自噪声。
        #
        # 所以下面那两组数字**必然**得出「无差异」，而这不是实验结论，
        # 是实验没做。运营如果照着报表决策会得出错误判断。
        # 恢复 A/B 语义需要先把 few-shot 选样按 user_id 分组（个人池优先），
        # 之后重跑积累足够样本，本条 caveat 才能撤掉。
        "⛔ 实验前提不成立：personalized / general 两组当前拿到完全相同的 few-shot "
        "样本（load_few_shots 不按 user_id 过滤），改写输入一致，本报表差异不可作为实验结论。",
        "0029 迁移上线前的历史蒸馏数据 ab_group=NULL，归入 pre_experiment 组，不可用于 A/B 结论",
        "A/B 结论需从 ab_group 落库部署日起重新计 2 周（方案 §2.7-D 周期）",
    ]
    ordered = [groups[k] for k in _GROUP_ORDER if k in groups]
    return {"groups": ordered, "caveats": caveats, "experiment_valid": False}
