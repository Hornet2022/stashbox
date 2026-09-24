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
#
# ⚠️ 这里的 `openai` 项写的是 **OpenAI 官方模型名**，只在 base_url 指向
# OpenAI 官方时成立。若管理后台把 llm 换成 OpenAI 兼容端点（火山方舟 /
# 自建代理等），必须同步在管理后台配 `tier-config`（「模型路由」页），
# 否则会回落到本默认值 → 模型名不被该供应商支持 → 蒸馏时 LLM 404。
#
# 2026-09-24 事故：管理后台把 llm 配成了火山方舟 + doubao-seed-2.0-lite，
# 但 tier 从未配置 →  distill_task 选 full tier 拿到 gpt-4o → 方舟返回
# `UnsupportedModel` 404，蒸馏 100% 失败。为消除这个陷阱，
# `resolve_tier_map()` 的 default 分支现在会**跟随当前 LLM 模型**
# （见 `_default_map_following_llm`）——「只改 llm 一处」也不再 404。
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

# OpenAI 官方 host：只有这些（或留空 = 默认官方）才认为 TIER_MODEL_MAP 的
# openai 模型名（gpt-4o 等）成立。其余一律视为 OpenAI 兼容第三方端点。
OPENAI_OFFICIAL_HOSTS: tuple[str, ...] = ("api.openai.com",)


def host_of(url: str | None) -> str:
    """取 URL 的 host（小写）；空/非法返回空串。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_openai_official(base_url: str | None) -> bool:
    """base_url 是否指向 OpenAI 官方（留空视为官方默认）。"""
    host = host_of(base_url)
    return host == "" or host in OPENAI_OFFICIAL_HOSTS


async def _default_map_following_llm() -> dict[str, dict[str, str]]:
    """代码默认 map；若当前 openai 供应商不是 OpenAI 官方，则把 openai 项
    统一替换为**当前生效 LLM 模型**。

    目的：让「换供应商只改管理后台 llm 一处」也不会在蒸馏时 404。
    读不到 llm 配置（无 Redis/PG 的测试环境）时按原样返回。
    """
    base = {tier: dict(provs) for tier, provs in TIER_MODEL_MAP.items()}
    try:
        from stashbox.backend.common.system_config import KEY_LLM, get_config

        llm_cfg = await get_config(KEY_LLM) or {}
    except Exception as exc:
        log.warning("tier_default_llm_read_failed", error=str(exc))
        return base

    provider = str(llm_cfg.get("provider") or "").lower()
    model = llm_cfg.get("model")
    base_url = llm_cfg.get("base_url")
    if provider == "openai" and model and not is_openai_official(base_url):
        for tier in base:
            base[tier]["openai"] = str(model)
        log.info(
            "tier_default_follows_llm_model",
            model=str(model),
            base_url=str(base_url),
            reason="base_url 不是 OpenAI 官方，避免硬编码 gpt-4o 被供应商拒（404）",
        )
    return base


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


# ── 动作 1：模型名 ↔ base_url 供应商一致性校验 ──────────────────────────
# 供应商 → 该供应商的模型名前缀特征。
# 只用于「显然不符」的拦截（如 gpt-4o 打到火山方舟），不做白名单限制，
# 未知供应商（自建代理等）一律放行，避免误伤。
VENDOR_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt", "text-davinci"),
    "volces": ("doubao-", "ep-"),
}


def infer_vendor(base_url: str | None) -> str | None:
    """从 base_url 推断 LLM 供应商；无法判断返回 None（调用方应放行）。"""
    host = host_of(base_url)
    if host == "" or host in OPENAI_OFFICIAL_HOSTS:
        return "openai"
    if "volces.com" in host:
        return "volces"
    return None


def check_model_matches_vendor(
    entry_provider: str,
    model: str,
    active_provider: str,
    base_url: str | None,
) -> str | None:
    """校验 tier map 里的模型名是否与当前 LLM 的 base_url 供应商显然不符。

    Args:
        entry_provider: 该 model 在 tier map 里所属的 provider 键（openai/qwen_vl/…）
        model: 模型名
        active_provider: 当前生效的 LLM provider（llm 配置里的 provider）
        base_url: 当前生效的 LLM base_url

    Returns:
        错误文案（应拒绝保存）；None = 通过。

    只校验「当前生效 provider」对应的项 —— 其余 provider 的 base_url 未知，
    校验它没有依据。供应商无法判断时一律放行。
    """
    if entry_provider != active_provider:
        return None
    vendor = infer_vendor(base_url)
    if vendor is None:
        return None
    foreign = [
        prefix
        for other_vendor, prefixes in VENDOR_MODEL_PREFIXES.items()
        if other_vendor != vendor
        for prefix in prefixes
    ]
    low = model.strip().lower()
    if any(low.startswith(p) for p in foreign):
        return (
            f"模型 '{model}' 属于其他供应商，但当前 LLM base_url 指向 "
            f"{vendor}（{base_url}）。请填该供应商支持的模型名，"
            f"否则蒸馏调用 LLM 会报 404 UnsupportedModel。"
        )
    return None


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

    default 分支说明（2026-09-24）：不再直接返回硬编码 `TIER_MODEL_MAP`，
    而是走 [_default_map_following_llm] —— 当 openai 的 base_url 不是
    OpenAI 官方时，openai 项会跟随当前 LLM 模型，避免「换供应商忘改 tier」
    导致蒸馏 404。

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
        return await _default_map_following_llm(), "default"

    # 部分覆盖：DB 只改了 simple 时，full 用「代码默认（必要时已跟随 llm）」补齐
    merged = await _default_map_following_llm()
    for tier, prov_map in validated.items():
        merged[tier].update(prov_map)
    return merged, "db"
