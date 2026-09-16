"""抖音内容抓取器（CP2.3 实现，本期只占位）。"""
from __future__ import annotations

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult

# 抖音的三种常见域名：
# - www.douyin.com   分享出来的视频/图文页
# - v.douyin.com     短链（会 302 到 www.douyin.com）
# - www.iesdouyin.com 老分享域名
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")


class DouyinFetcher(Fetcher):
    """抖音视频/图文抓取（CP2.3 实现）。

    CP2.3 要先解短链（v.douyin.com → www.douyin.com），再取页面里的
    视频直链 / 图集；抖音正文常常只在 JS 渲染后的数据里，需要留意。
    """

    @property
    def name(self) -> str:
        return "douyin"

    def supports(self, url: str) -> bool:
        return any(host in url for host in DOUYIN_HOSTS)

    async def fetch(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        # CP2.1 占位，真抓取留给 CP2.3
        raise FetcherError(
            code=FetcherErrorCode.UNSUPPORTED,
            message="DouyinFetcher 尚未实现，留给 CP2.3",
            source=self.name,
        )
