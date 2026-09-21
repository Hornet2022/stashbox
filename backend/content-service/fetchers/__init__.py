"""content-service 抓取器包（v1 §11.2 CP2.1）。

公共导出 + 工厂 get_fetcher()。CP2.1 只建抽象层，还没接入主流程
（接入在 CP2.5 服务号 Handler / CP2.7 集成测）。
"""
from __future__ import annotations

from stashbox.backend.common.exceptions import BizException

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult
from .douyin import DouyinFetcher
from .generic_url import GenericURLFetcher
from .pdf import PdfFetcher
from .wechat import WechatFetcher

# 顺序 = 优先级：专属域名在前，通用兜底在后
_ALL_FETCHERS: list[Fetcher] = [
    WechatFetcher(),
    DouyinFetcher(),
    # CP11.0.8 P3.3: PDF 直链（URL path 以 .pdf 结尾才接）
    PdfFetcher(),
    GenericURLFetcher(),  # 通用放最后（catch-all，优先级最低）
]


def get_fetcher(url: str) -> Fetcher | None:
    """按 URL 匹配一个 fetcher（CP2.5 服务号 Handler 会用）。

    Returns:
        第一个 supports(url) 为 True 的 fetcher；理论上不会返回 None
        （GenericURLFetcher 是 catch-all）。
    """
    for fetcher in _ALL_FETCHERS:
        if fetcher.supports(url):
            return fetcher
    return None


def map_fetcher_error(exc: FetcherError) -> BizException:
    """FetcherError → BizException（v1 §3.x 文章模块错误码）。

    fetcher 私有错误码（fetcher.*）→ 业务错误码的**唯一映射点**，不散在 Handler 里：
    - UNSUPPORTED → 2001（URL 不支持，HTTP 400）
    - NETWORK/PARSE/NOT_FOUND/AUTH/RATE_LIMIT → 2002（抓取失败，HTTP 502）
    - INTERNAL → 2002（抓取失败，HTTP 500）
    """
    if exc.code == FetcherErrorCode.UNSUPPORTED:
        return BizException(code=2001, message=f"url not supported: {exc.message}")

    biz = BizException(
        code=2002,
        message=f"fetch failed [{exc.source}/{exc.code.value}]: {exc.message}",
    )
    biz.http_status = 500 if exc.code == FetcherErrorCode.INTERNAL else 502
    return biz


__all__ = [
    "Fetcher",
    "FetchResult",
    "FetcherError",
    "FetcherErrorCode",
    "WechatFetcher",
    "DouyinFetcher",
    "GenericURLFetcher",
    "get_fetcher",
    "map_fetcher_error",
]
