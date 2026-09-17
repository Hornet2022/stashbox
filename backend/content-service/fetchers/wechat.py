"""微信公众号文章抓取（v1 §11.2 CP2.2）。

微信公众号正文页结构固定，所以不用通用 ContentExtractor 的密度启发式，直接按专属选择器取：

- 正文：`<div id="js_content">...</div>`（公众号正文的唯一容器）
- 标题：`<title>标题 - 公众号名</title>`（要剥掉 " - 公众号名" 后缀）
- 作者：`<a id="js_name">公众号名</a>` / `<meta name="author">`
- 发布时间：`<em id="publish_time">2026-01-01 12:34</em>` / `<meta property="article:published_time">`
- 图片：`<img data-src="...">`（公众号图片默认懒加载，`data-src` 才是真图链）

反爬机制（本期只做识别，不做破解）：

- "环境异常" 页面 → AUTH（微信风控判定非真人环境）
- "请在微信中打开" 提示页 → AUTH（必须在微信内置浏览器）
- "该公众号已迁移" → NOT_FOUND
- "此内容因违规无法查看" → AUTH

本期只发 iPhone UA + Referer 单实例抓取，**不做代理池 / cookie 池**。
如果未来反爬加严，扩展点是 `WechatFetcher._download()`（加代理 / cookie），
不是改 fetcher 抽象层（CP2.1 契约定死）。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .base import (
    FetchResult,
    Fetcher,
    FetcherError,
    FetcherErrorCode,
)
from .parser import (
    AuthorExtractor,
    MediaExtractor,
    TimeExtractor,
    TitleExtractor,
    _norm,
    _parse_datetime,
)

# 公众号文章域名：正文页 mp.weixin.qq.com
WECHAT_HOST = "mp.weixin.qq.com"
SOURCE = "wechat_mp"

# 正文容器（非贪婪到紧跟的 </div>，公众号 js_content 后面通常直接跟 <script>）
_JS_CONTENT_RE = re.compile(
    r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>\s*<script', re.DOTALL | re.IGNORECASE
)
# 兜底：页面在 js_content 后没紧跟 <script> 时，一路吃到文档末尾再截断
_JS_CONTENT_FALLBACK_RE = re.compile(
    r'<div[^>]*id=["\']js_content["\'][^>]*>(.*)', re.DOTALL | re.IGNORECASE
)
_JS_NAME_RE = re.compile(r'<a[^>]*id=["\']js_name["\'][^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_PUBLISH_TIME_RE = re.compile(
    r'<em[^>]*id=["\']publish_time["\'][^>]*>(.*?)</em>', re.DOTALL | re.IGNORECASE
)
_DATA_SRC_RE = re.compile(r'<img[^>]*\bdata-src=["\']([^"\']+)["\']', re.IGNORECASE)
# <title> 的常见后缀："标题 - 公众号名" / "标题 | 公众号名"
_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*[^|\-–—]{1,30}$")

_WECHAT_BLOCKLIST_PATTERNS: list[tuple[str, FetcherErrorCode, str]] = [
    ("环境异常", FetcherErrorCode.AUTH, "wechat anti-bot environment check"),
    ("请在微信中打开", FetcherErrorCode.AUTH, "wechat requires in-app browser"),
    ("该公众号已迁移", FetcherErrorCode.NOT_FOUND, "wechat account migrated"),
    ("此内容因违规无法查看", FetcherErrorCode.AUTH, "wechat content blocked"),
]


def _check_wechat_block(html: str) -> None:
    """命中公众号反爬 / 失效提示页 → 抛 FetcherError（AUTH / NOT_FOUND）。"""
    for pattern, code, msg in _WECHAT_BLOCKLIST_PATTERNS:
        if re.search(pattern, html):
            raise FetcherError(code=code, message=msg, source=SOURCE)


class _TagStripper(HTMLParser):
    """HTML 片段 → 纯文本（跳过 script/style，块级标签补换行）。"""

    _BREAK_TAGS = frozenset({"br", "p", "div", "section", "li", "tr", "h1", "h2", "h3", "h4"})
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if not self._skip_depth and tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _strip_tags(html: str) -> str:
    """去掉标签，返回带换行的纯文本（再交给 `_norm` 折叠空白）。"""
    stripper = _TagStripper()
    stripper.feed(html)
    stripper.close()
    return stripper.text()


def _clean_title(raw: str, account_name: str = "") -> str:
    """剥掉公众号 `<title>` 的 " - 公众号名" 后缀（保留标题本体）。"""
    title = _norm(raw)
    if account_name and title.endswith(account_name):
        head = title[: -len(account_name)].rstrip().rstrip("|-–— ")
        if head:
            return _norm(head)
    stripped = _TITLE_SUFFIX_RE.sub("", title)
    return _norm(stripped) or title


class WechatFetcher(Fetcher):
    """微信公众号文章抓取（CP2.2 实现）。

    两种入口都走同一条路径：
    - 分享出来的文章链接：https://mp.weixin.qq.com/s/xxx
    - 二维码长按场景：二维码解出的 URL 也是 mp.weixin.qq.com
    """

    UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1 WechatFetcher/0.1"
    )
    TIMEOUT = 30.0
    MAX_HTML_CHARS = 2 * 1024 * 1024  # 解析前 HTML 截断（对齐 generic_url 的防爆上限）

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 只是测试注入点（httpx.MockTransport），生产走默认 transport
        self._transport = transport

    @property
    def name(self) -> str:
        return SOURCE

    def supports(self, url: str) -> bool:
        return WECHAT_HOST in url

    async def fetch(self, url: str, *, timeout: float = TIMEOUT) -> FetchResult:
        # 非公众号 URL 不发请求（CP2.1 契约：supports() 说了算），否则会被厂商当爬虫
        if not self.supports(url):
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"unsupported url: {url!r}",
                source=self.name,
            )
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"unsupported url scheme: {url!r}",
                source=self.name,
            )

        html, status_code, final_url = await self._download(url, timeout=timeout)
        _check_wechat_block(html)
        return self.parse_article(
            html, url=url, final_url=final_url, status_code=status_code
        )

    # -- 下载 ---------------------------------------------------------------
    async def _download(self, url: str, *, timeout: float) -> tuple[str, int, str]:
        """抓 HTML：iPhone UA + Referer（公众号对外站 UA 敏感），返回 (html, status, final_url)。"""
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.UA,
                "Referer": "https://mp.weixin.qq.com/",
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

        html = response.text[: self.MAX_HTML_CHARS]
        if not html.strip():
            raise FetcherError(
                code=FetcherErrorCode.PARSE, message="empty html body", source=self.name
            )
        return html, response.status_code, str(response.url)

    # -- 解析 ---------------------------------------------------------------
    def parse_article(
        self, html: str, *, url: str, final_url: str, status_code: int
    ) -> FetchResult:
        """纯函数：HTML → FetchResult（测试和真抓取共用同一条解析路径）。"""
        try:
            title_ex = TitleExtractor().parse(html)
            author_ex = AuthorExtractor().parse(html)
            time_ex = TimeExtractor().parse(html)
            media_ex = MediaExtractor(base_url=final_url).parse(html)
        except Exception as exc:  # HTMLParser 理论上不抛，兜底防脏 HTML 炸主流程
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"html parse failed: {type(exc).__name__}: {exc}",
                source=self.name,
            ) from exc

        account_name = self._account_name(html)

        # author：<meta name="author"> 优先（真作者），退回公众号名
        author = author_ex.best or account_name or None

        # publish_time：meta 优先，退回 <em id="publish_time">（"2026-09-17 08:30" 无时区按 UTC）
        publish_time = time_ex.best
        publish_time_raw = time_ex.raw
        time_match = _PUBLISH_TIME_RE.search(html)
        if time_match:
            raw_local = _norm(time_match.group(1))
            if publish_time is None:
                publish_time = _parse_datetime(raw_local.replace(" ", "T"))
                publish_time_raw = raw_local

        # media：og:image + 正文里懒加载的 <img data-src>（公众号真图链接）
        media_urls = list(media_ex.urls)
        for src in _DATA_SRC_RE.findall(html):
            absolute = urljoin(final_url, src.strip())
            if absolute not in media_urls:
                media_urls.append(absolute)

        # 正文：公众号固定容器 #js_content
        content_html = self._extract_body(html)
        content_text = _norm(_strip_tags(content_html))
        if not content_text:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="wechat article body (#js_content) is empty",
                source=self.name,
            )

        return FetchResult(
            url=url,
            title=_clean_title(title_ex.best or "", account_name) or final_url,
            content_html=content_html,
            content_text=content_text,
            author=author,
            publish_time=publish_time,
            media_urls=media_urls,
            source=self.name,
            raw_metadata={
                "wechat_id": account_name,
                "author_url": final_url,
                "final_url": final_url,
                "status_code": status_code,
                "og_title": title_ex.og_title,
                "html_title": title_ex.title,
                "publish_time_raw": publish_time_raw,
            },
        )

    @staticmethod
    def _account_name(html: str) -> str:
        """公众号名（`<a id="js_name">`），拿不到返回空串。"""
        match = _JS_NAME_RE.search(html)
        return _norm(match.group(1)) if match else ""

    @staticmethod
    def _extract_body(html: str) -> str:
        """取 `<div id="js_content">` 内容；拿不到就是反爬页 / 结构变了 → PARSE。"""
        match = _JS_CONTENT_RE.search(html) or _JS_CONTENT_FALLBACK_RE.search(html)
        if not match:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="wechat article body (#js_content) not found",
                source=SOURCE,
            )
        return match.group(1).strip()


__all__ = ["SOURCE", "WECHAT_HOST", "WechatFetcher", "_check_wechat_block"]
