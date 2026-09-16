"""通用 URL 抓取器（CP2.4 实现，本期只占位）。"""
from __future__ import annotations

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult


class GenericURLFetcher(Fetcher):
    """通用网页抓取（CP2.4 实现）。

    catch-all：supports() 永远 True，所以在 get_fetcher() 里必须排在最后 ——
    否则会抢走公众号/抖音的 URL。
    """

    @property
    def name(self) -> str:
        return "generic_url"

    def supports(self, url: str) -> bool:
        return True

    async def fetch(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        # CP2.1 占位，真抓取留给 CP2.4
        raise FetcherError(
            code=FetcherErrorCode.UNSUPPORTED,
            message="GenericURLFetcher 尚未实现，留给 CP2.4",
            source=self.name,
        )
