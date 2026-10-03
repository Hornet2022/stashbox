"""标准降级通道的组装（HTTP → 浏览器 → 代理预留）。

把"这一节通道要发什么请求、超时怎么算"收敛在这里，好处是各 fetcher 的 `fetch()`
只剩一行调用 —— 通道编排逻辑**只有一份**，新增 fetcher 不可能漏配。

各 fetcher 只负责提供自己的 `http` 通道（站点专属的 URL 校验 / 状态码判定），
浏览器通道是站点无关的，所以由这里统一提供。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from .browser import browser_tier_enabled, get_renderer
from .net import Deadline, HeaderProfile
from .pipeline import BROWSER_TIER_CAP, Acquired, Tier

log = logging.getLogger("stashbox.fetch.tiers")


def _mark_browser_unavailable() -> None:
    """浏览器通道不可用就打点 —— 线上**少了一层兜底**，这类静默降级必须有指标。

    只计"不可用"，不记"可用"：rate() 为 0 就说明通道健康，
    而"可用次数"这种分母会随流量涨落，看了也不知道该修什么。
    """
    from stashbox.backend.common import fetch_metrics

    fetch_metrics.fetch_browser_tier_unavailable_total.inc()


def browser_acquirer(
    url: str,
    *,
    profile: HeaderProfile,
    deadline: Deadline,
) -> Callable[[], Awaitable[Acquired]]:
    """构造浏览器通道。

    UA 必须取**和 HTTP 通道同一个画像**：浏览器里 JS 读到的 navigator.userAgent
    与 HTTP 头对不上，是比 headless 本身更容易被抓的破绽。

    不可用时抛 `BrowserUnavailable`（不是 FetcherError）—— pipeline 会把它
    当"这节通道不存在"静默跳过。剪藏成功率不能因为一个可选依赖而下降。
    """
    from .browser import BrowserUnavailable
    from .net import build_headers

    user_agent = build_headers(profile)["User-Agent"]

    async def _acquire() -> Acquired:
        if not browser_tier_enabled():
            raise BrowserUnavailable("browser tier disabled by config")
        budget = deadline.slice(BROWSER_TIER_CAP) or 0.0
        if budget <= 0.0:
            raise BrowserUnavailable("no budget left for browser tier")
        try:
            page = await get_renderer().render(url, user_agent=user_agent, timeout=budget)
        except BrowserUnavailable:
            # 打点：浏览器通道静默不可用 = 线上**少了一层兜底**。
            # 这类降级如果只有日志没人看，等于悄悄丢掉了剪藏成功率。
            _mark_browser_unavailable()
            raise
        if page.status is not None and page.status >= 400:
            # 渲染的是错误页，交回状态码让 pipeline 用统一的错误码语义去判
            from .base import FetcherError, FetcherErrorCode

            if page.status in (404, 410):
                raise FetcherError(
                    code=FetcherErrorCode.NOT_FOUND,
                    message=f"http {page.status}",
                    source="browser",
                )
            if page.status == 429:
                raise FetcherError(
                    code=FetcherErrorCode.RATE_LIMIT,
                    message="http 429",
                    source="browser",
                )
            raise FetcherError(
                code=FetcherErrorCode.NETWORK, message=f"http {page.status}", source="browser"
            )
        return Acquired(
            html=page.html, final_url=page.final_url, status=page.status, tier="browser"
        )

    return _acquire


def standard_tiers(
    url: str,
    *,
    http: Callable[[], Awaitable[Acquired]],
    profile: HeaderProfile,
    deadline: Deadline,
    with_browser: bool = True,
) -> list[Tier]:
    """标准通道列表。

    `with_browser=False` 的场景：注入了 MockTransport 的测试（不该起真浏览器），
    以及某些明确不该走浏览器的路径。
    """
    tiers = [Tier(name="http", acquire=http)]
    if with_browser:
        tiers.append(
            Tier(name="browser", acquire=browser_acquirer(url, profile=profile, deadline=deadline))
        )
    return tiers


def host_of(url: str) -> str:
    """取 host（节流用；解析失败时退化成整串 URL 的短前缀，避免空 key 撞在一起）。"""
    try:
        return urlparse(url).netloc or url[:40]
    except ValueError:
        return url[:40]


__all__ = ["browser_acquirer", "host_of", "standard_tiers"]
