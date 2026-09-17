"""通用 URL 抓取器（v1 §11.2 CP2.4）。

任意网页的正文抽取，readability 简化启发式（纯 stdlib + httpx，不装 beautifulsoup4）。

步骤：
1. httpx.AsyncClient 抓 HTML（含 redirect + UA + timeout）
2. 检查 Content-Type（必须 text/html / application/xhtml+xml）
3. TitleExtractor / AuthorExtractor / TimeExtractor / MediaExtractor（见 .parser）
4. ContentExtractor 抽正文（密度最高的 <p> 段落，按文档顺序输出）
5. 返回 FetchResult

失败一律抛 FetcherError（NETWORK / PARSE / NOT_FOUND / UNSUPPORTED）。

解析部分（5 个 HTMLParser + 3 个工具函数）CP2.2 已抽到 `fetchers/parser.py`，
本模块 `from .parser import ...` 复用，公众号抓取器共用同一套解析器。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from .base import (
    FetchResult,
    Fetcher,
    FetcherError,
    FetcherErrorCode,
)
from .parser import (
    MAX_HTML_CHARS,
    MIN_TEXT_DENSITY,
    AuthorExtractor as _AuthorExtractor,
    ContentExtractor as _ContentExtractor,
    MediaExtractor as _MediaExtractor,
    TimeExtractor as _TimeExtractor,
    TitleExtractor as _TitleExtractor,
)

_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})  # 本模块专属，不进 parser

class GenericURLFetcher(Fetcher):
    """通用 URL 抓取（任意网页正文抽取）。

    catch-all：supports() 永远 True，所以在 get_fetcher() 里必须排在最后 ——
    否则会抢走公众号/抖音的 URL。
    """

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StashBox/0.1"
    )
    TIMEOUT = 30.0

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 只是测试注入点（httpx.MockTransport），生产走默认 transport
        self._transport = transport

    @property
    def name(self) -> str:
        return "generic_url"

    def supports(self, url: str) -> bool:
        return True  # catch-all

    async def fetch(self, url: str, *, timeout: float = TIMEOUT) -> FetchResult:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"unsupported url scheme: {url!r}",
                source=self.name,
            )

        # 1. HTTP 抓取
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"timeout after {timeout}s",
                source=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"request failed: {type(exc).__name__}: {exc}",
                source=self.name,
            ) from exc

        # 404/410 是"文章没了"，5xx 和其它 4xx 归到网络/站点侧
        if response.status_code in (404, 410):
            raise FetcherError(
                code=FetcherErrorCode.NOT_FOUND,
                message=f"http {response.status_code}",
                source=self.name,
            )
        if response.status_code >= 400:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"http {response.status_code}",
                source=self.name,
            )

        # 2. Content-Type 校验（缺失时按 HTML 宽容处理）
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and content_type not in _HTML_CONTENT_TYPES:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"content-type not html: {content_type!r}",
                source=self.name,
            )

        # 相对路径要按"最终 URL"（redirect 之后）解析
        final_url = str(response.url)
        html = response.text[:MAX_HTML_CHARS]
        if not html.strip():
            raise FetcherError(
                code=FetcherErrorCode.PARSE, message="empty html body", source=self.name
            )

        # 3 + 4. 5 个 stdlib HTMLParser 抽元数据 + 正文
        try:
            title_ex = _TitleExtractor().parse(html)
            author_ex = _AuthorExtractor().parse(html)
            time_ex = _TimeExtractor().parse(html)
            media_ex = _MediaExtractor(base_url=final_url).parse(html)
            content_ex = _ContentExtractor().parse(html)
        except Exception as exc:  # HTMLParser 理论上不抛，兜底防脏 HTML 炸主流程
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"html parse failed: {type(exc).__name__}: {exc}",
                source=self.name,
            ) from exc

        content_text = content_ex.content_text()
        if not content_text:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="no article content extracted",
                source=self.name,
            )

        # 5. 构造 FetchResult
        return FetchResult(
            url=url,
            title=title_ex.best or final_url,
            content_html=content_ex.content_html(),
            content_text=content_text,
            author=author_ex.best,
            publish_time=time_ex.best,
            media_urls=media_ex.urls,
            source=self.name,
            raw_metadata={
                "final_url": final_url,
                "status_code": response.status_code,
                "content_type": content_type,
                "og_title": title_ex.og_title,
                "html_title": title_ex.title,
                "publish_time_raw": time_ex.raw,
                "paragraph_count": len(content_ex.best()),
                "candidate_count": len(content_ex.blocks),
            },
        )


# CP2.4 测试按 `_XxxExtractor` 旧名取解析器（`gu._TitleExtractor`），这里显式再导出。
__all__ = [
    "GenericURLFetcher",
    "MAX_HTML_CHARS",
    "MIN_TEXT_DENSITY",
    "_AuthorExtractor",
    "_ContentExtractor",
    "_MediaExtractor",
    "_TimeExtractor",
    "_TitleExtractor",
]
