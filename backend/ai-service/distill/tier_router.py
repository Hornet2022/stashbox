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
    """根据 tier + provider 选模型字符串（代码默认映射）。

    Args:
        tier: 'simple' / 'full'
        provider: 'openai' / 'qwen_vl' / 'claude'

    Returns:
        模型字符串（如 'gpt-4o-mini' / 'qwen-vl-max'）

    Raises:
        KeyError: tier 或 provider 不在 TIER_MODEL_MAP 中
    """
    return TIER_MODEL_MAP[tier][provider]


def _validate_tier_map(candidate: object) -> dict[str, dict[str, str]] | None:
    """校验 DB 存的 tier map 形状：{simple|full: {provider: 非空字符串}}。

    合法返回规范化 dict；非法返回 None（调用方回退代码默认）。
    """
    if not isinstance(candidate, dict):
        return None
    result: dict[str, dict[str, str]] = {}
    for tier, prov_map in candidate.items():
        if tier not in ("simple", "full") or not isinstance(prov_map, dict):
            return None
        norm: dict[str, str] = {}
        for provider, model in prov_map.items():
            if not isinstance(model, str) or not model.strip():
                return None
            norm[str(provider)] = model.strip()
        if not norm:
            return None
        result[tier] = norm
    return result or None


async def resolve_tier_map() -> tuple[dict[str, dict[str, str]], str]:
    """B3/缺口 A1 + D2：生效 tier→model 映射 = DB（system_config KEY_TIER）> 代码默认。

    Redis 5s 缓存由 system_config.get_config 统一提供；蒸馏任务无需重启即生效。

    Returns:
        (effective_map, source)  source ∈ {"db", "default"}
    """
    try:
        from stashbox.backend.common.system_config import KEY_TIER, get_config

        stored = await get_config(KEY_TIER)
    except Exception as e:
        # 本机无 Redis/PG（测试环境）→ 静默回退代码默认
        log.warning("tier_map_db_read_failed_use_default", error=str(e))
        return TIER_MODEL_MAP, "default"

    candidate = stored.get("tier_model_map") if isinstance(stored, dict) else None
    validated = _validate_tier_map(candidate)
    if validated is None:
        if stored:
            log.warning("tier_map_db_invalid_use_default", stored=str(stored)[:200])
        return TIER_MODEL_MAP, "default"

    # 部分覆盖：DB 只改了 simple 时，full 仍用代码默认补齐
    merged = {t: dict(provs) for t, provs in TIER_MODEL_MAP.items()}
    for tier, prov_map in validated.items():
        merged[tier].update(prov_map)
    return merged, "db"
