"""content-service 抓取器包（v1 §11.2 CP2.1）。

公共导出 + 工厂 get_fetcher()。CP2.1 只建抽象层，还没接入主流程
（接入在 CP2.5 服务号 Handler / CP2.7 集成测）。
"""
from __future__ import annotations

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult
from .douyin import DouyinFetcher
from .generic_url import GenericURLFetcher
from .wechat import WechatFetcher

# 顺序 = 优先级：专属域名在前，通用兜底在后
_ALL_FETCHERS: list[Fetcher] = [
    WechatFetcher(),
    DouyinFetcher(),
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


__all__ = [
    "Fetcher",
    "FetchResult",
    "FetcherError",
    "FetcherErrorCode",
    "WechatFetcher",
    "DouyinFetcher",
    "GenericURLFetcher",
    "get_fetcher",
]
