"""微信公众号文章抓取器（CP2.2 实现，本期只占位）。"""
from __future__ import annotations

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult

# 公众号文章域名：正文页 mp.weixin.qq.com
WECHAT_HOST = "mp.weixin.qq.com"


class WechatFetcher(Fetcher):
    """微信公众号文章抓取（CP2.2 实现）。

    CP2.2 要处理的两种入口：
    - 分享出来的文章链接：https://mp.weixin.qq.com/s/xxx
    - 二维码长按场景：二维码解出的 URL 也是 mp.weixin.qq.com，走同一条路径
    """

    @property
    def name(self) -> str:
        return "wechat_mp"

    def supports(self, url: str) -> bool:
        return WECHAT_HOST in url

    async def fetch(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        # CP2.1 占位，真抓取留给 CP2.2
        raise FetcherError(
            code=FetcherErrorCode.UNSUPPORTED,
            message="WechatFetcher 尚未实现，留给 CP2.2",
            source=self.name,
        )
