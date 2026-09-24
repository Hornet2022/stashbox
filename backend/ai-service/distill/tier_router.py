"""CP3.7.2 §2.2.D：tier_router（4 条规则 + TIER_MODEL_MAP）。

按 docs/听感产品化方案_v1.md §2.2.D 严格实现 4 条规则：
1. 强制 full：用户订阅 pro/member + 反馈数 >= 10 + 历史评分均值 < 3.5
2. 强制 full：article.raw_content 字数 > 3000 OR 来源 PDF/arxiv/long_wechat
3. simple：article.raw_content 字数 < 800 AND 来源抖音/快讯 AND 用户评分均值 >= 4.0
4. 默认 full（保守）

失败兜底：异常 → 返回 "full"。
"""

from __future__ import annotations

from typing import Literal

import structlog

from .schemas import UserListeningPattern

log = structlog.get_logger("distill.tier_router")

Tier = Literal["simple", "full"]


async def route_tier(
    article: object,  # stashbox.backend.common.models.Article
    user: object,  # stashbox.backend.common.models.User
    user_profile: UserListeningPattern | None,
    db: object,  # AsyncSession
) -> Tier:
    """根据文章 + 用户画像选模型 tier。

    4 条规则按优先级匹配，第一个命中即返回。
    失败兜底：异常 → 返回 "full"（保守，不降低听感）。
    """
    try:
        # 规则 1：pro/member 用户 + 反馈数 >= 10 + 历史评分均值 < 3.5 → 强制 full
        user_tier = getattr(user, "tier", None)
        if user_tier in ("pro", "member") and user_profile and user_profile.feedback_count >= 10:
            avg = user_profile.avg_overall_score
            if avg is not None and avg < 3.5:
                log.info(
                    "tier_route_force_full_low_score",
                    user_tier=user_tier,
                    feedback_count=user_profile.feedback_count,
                    avg_score=avg,
                )
                return "full"

        # 规则 2：长文 / PDF / arxiv → 强制 full
        raw_content = getattr(article, "raw_content", None)
        if raw_content:
            if isinstance(raw_content, dict):
                content_text = raw_content.get("content_text", "") or ""
            else:
                content_text = str(raw_content)
            if len(content_text) > 3000:
                return "full"
        source_type = getattr(article, "source_type", None) or getattr(article, "source", None)
        if source_type in ("pdf", "arxiv", "long_wechat"):
            return "full"

        # 规则 3：抖音/快讯短文 + 用户评分均值 >= 4.0 → simple
        if raw_content:
            if isinstance(raw_content, dict):
                content_text = raw_content.get("content_text", "") or ""
            else:
                content_text = str(raw_content)
            if len(content_text) < 800 and source_type in ("douyin", "short_news"):
                if (
                    user_profile
                    and user_profile.avg_overall_score is not None
                    and user_profile.avg_overall_score >= 4.0
                ):
                    log.info(
                        "tier_route_simple_douyin_high_score",
                        source_type=source_type,
                        avg_score=user_profile.avg_overall_score,
                    )
                    return "simple"

        # 规则 4：默认 full（保守）
        return "full"
    except Exception as e:
        # CP3.7.2 §2.2.D 兜底：异常 → full（不降低听感）
        log.warning("tier_route_failed_fallback_full", error=str(e))
        return "full"


# TIER_MODEL_MAP：CP3.6.2 配合
# simple：轻量模型（成本 1/5，听感中等）
# full：完整模型（成本高，听感优）
TIER_MODEL_MAP: dict[str, dict[str, str]] = {
    "simple": {
        "openai": "gpt-4o-mini",
        "qwen_vl": "qwen2.5-7b-instruct",
        "claude": "claude-3-5-haiku-20241022",
    },
    "full": {
        "openai": "gpt-4o",
        "qwen_vl": "qwen-vl-max",
        "claude": "claude-4-sonnet-20250514",
    },
}


def get_model_for_tier(tier: str, provider: str) -> str:
    """根据 tier + provider 选模型字符串。

    Args:
        tier: 'simple' / 'full'
        provider: 'openai' / 'qwen_vl' / 'claude'

    Returns:
        模型字符串（如 'gpt-4o-mini' / 'qwen-vl-max'）

    Raises:
        KeyError: tier 或 provider 不在 TIER_MODEL_MAP 中
    """
    return TIER_MODEL_MAP[tier][provider]
