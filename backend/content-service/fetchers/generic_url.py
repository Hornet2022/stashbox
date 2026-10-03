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

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from stashbox.backend.common.ssrf import SsrfBlocked, assert_public_url

from .base import (
    FetchResult,
    Fetcher,
    FetcherError,
    FetcherErrorCode,
)

from .net import (
    Deadline,
    HeaderProfile,
    HOST_THROTTLE,
    acquire_client,
    build_client_kwargs,
    build_headers,
    fetch_with_retry,
    to_fetcher_error,
)
from .parser import (
    MAX_HTML_CHARS,
    MIN_ARTICLE_CHARS,
    MIN_TEXT_DENSITY,
    AuthorExtractor as _AuthorExtractor,
    ContentExtractor as _ContentExtractor,
    MediaExtractor as _MediaExtractor,
    TimeExtractor as _TimeExtractor,
    TitleExtractor as _TitleExtractor,
)
from .pipeline import Acquired, fetch_with_escalation
from .tiers import host_of, standard_tiers

log = logging.getLogger("stashbox.fetch.generic_url")

#: 429 单列一个码：它的重试语义（换出口 IP / 降频）和 5xx 完全不同
_RATE_LIMIT_STATUS = 429
#: 401/403 = 需要登录或被风控挡在门外 → AUTH（可升级到浏览器通道重试）
_AUTH_STATUSES = (401, 403)


async def _ssrf_request_hook(request: httpx.Request) -> None:
    """把 SsrfBlocked 翻成 httpx 能识别的异常类型，让 fetcher 的错误分支接住。

    必须写成 async：httpx 的 AsyncClient 会 ``await hook(request)``，传同步函数
    会直接 TypeError（同步 Client 不 await，所以这个区别很容易踩）。

    DNS 解析（socket.getaddrinfo）是阻塞调用，放进 to_thread 免得把事件循环卡住。

    ⚠️ 保留这个函数只为兼容旧引用（`gu._ssrf_request_hook`）；真实挂载点已经收进
    `net.build_client_kwargs()`，现在 wechat / douyin / pdf 也自动带上防护
    （改造前只有 generic_url 有，另外三个是裸的 —— 见 net.py 模块顶部安全说明）。
    """
    try:
        await asyncio.to_thread(assert_public_url, str(request.url))
    except SsrfBlocked as exc:
        raise httpx.RequestError(f"blocked target: {exc}", request=request) from exc


_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})  # 本模块专属，不进 parser


class GenericURLFetcher(Fetcher):
    """通用 URL 抓取（任意网页正文抽取）。

    catch-all：supports() 永远 True，所以在 get_fetcher() 里必须排在最后 ——
    否则会抢走公众号/抖音的 URL。
    """

    UA = build_headers(HeaderProfile.DESKTOP)["User-Agent"]
    PROFILE = HeaderProfile.DESKTOP
    TIMEOUT = 30.0

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 只是测试注入点（httpx.MockTransport），生产走默认 transport
        self._transport = transport

    @property
    def name(self) -> str:
        return "generic_url"

    def supports(self, url: str) -> bool:
        return True  # catch-all

    async def fetch(
        self, url: str, *, timeout: float = TIMEOUT, budget: float | None = None
    ) -> FetchResult:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"unsupported url scheme: {url!r}",
                source=self.name,
            )

        # SSRF 防护统一在 net.build_client_kwargs()（request hook 在每次请求**发出
        # 之前**触发，且重定向每一跳都会重新触发，见 common/ssrf.py 时序记录）。
        # 刻意**不**在这里再做一次前置校验：实测 httpx 0.28.1 的 request hook 在
        # 首个请求之前就会触发（不只是重定向跳），所以前置校验是纯冗余 ——
        # 而冗余的防护如果没有任何测试覆盖，就等于给后来人一个「已经校验过了」的
        # 错觉，正是本项目反复吃过亏的那类假守卫。单一拦截点 = 单一可测事实。
        deadline = Deadline(budget)
        return await fetch_with_escalation(
            url=url,
            parse=self.parse,
            source=self.name,
            deadline=deadline,
            tiers=standard_tiers(
                url,
                http=lambda: self._acquire(url, timeout=timeout),
                profile=self.PROFILE,
                deadline=deadline,
                with_browser=self._transport is None,
            ),
        )

    def parse(self, acq: Acquired) -> FetchResult:
        """纯解析：HTML → FetchResult。浏览器通道拿到的 HTML 也走这里。

        与 wechat 的 `parse_article` 同构：解析逻辑只写一份，通道只管"怎么拿到 HTML"。
        """
        html = acq.html[:MAX_HTML_CHARS]
        final_url = acq.final_url
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
        # 合理性闸门：密度启发式**只管单段**，抓不到"整篇其实是壳"这种情况。
        # 实测 `<div id="app">loading</div>` 密度 0.28 > 0.25 会被当成合法正文，
        # 于是 JS 占位页"抓取成功"且正文只有 7 个字（见 parser.MIN_ARTICLE_CHARS）。
        # 抛 PARSE 而不是返回一个假成功 —— PARSE 是可升级错误码，
        # 正好触发降级链去试无头浏览器通道把真正文渲染出来。
        if len(content_text) < MIN_ARTICLE_CHARS:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=(
                    f"extracted content too short: {len(content_text)} chars "
                    f"< {MIN_ARTICLE_CHARS} — 页面可能是 JS 渲染或需登录才出正文"
                ),
                source=self.name,
            )

        # 5. 构造 FetchResult
        return FetchResult(
            url=acq.url or final_url,
            title=title_ex.best or final_url,
            content_html=content_ex.content_html(),
            content_text=content_text,
            author=author_ex.best,
            publish_time=time_ex.best,
            media_urls=media_ex.urls,
            source=self.name,
            raw_metadata={
                "final_url": final_url,
                "status_code": acq.status,
                "content_type": acq.extra.get("content_type", ""),
                "og_title": title_ex.og_title,
                "html_title": title_ex.title,
                "publish_time_raw": time_ex.raw,
                "paragraph_count": len(content_ex.best()),
                "candidate_count": len(content_ex.blocks),
            },
        )

    # -- 下载 ---------------------------------------------------------------
    async def _acquire(self, url: str, *, timeout: float) -> Acquired:
        """降级链的 HTTP 通道：节流 → 退避重试 → 状态码 / Content-Type 判定。"""
        html, status, final_url, content_type = await self._download(url, timeout=timeout)
        return Acquired(
            html=html,
            final_url=final_url,
            status=status,
            tier="http",
            url=url,
            extra={"content_type": content_type},
        )

    async def _download(self, url: str, *, timeout: float) -> tuple[str, int, str, str]:
        """抓 HTML，返回 (html, status, final_url, content_type)。"""
        await HOST_THROTTLE.wait(host_of(url))
        client_kwargs = build_client_kwargs(
            self.PROFILE,
            guard_ssrf=self._transport is None,
            transport=self._transport,
        )
        client = acquire_client(self.PROFILE, **client_kwargs)

        async def send(per_attempt: float) -> httpx.Response:
            return await client.get(url, timeout=per_attempt)

        try:
            response = await fetch_with_retry(
                send, on_retry=lambda n, why: log.info("generic_retry attempt=%d %s", n, why)
            )
        except httpx.HTTPError as exc:
            raise to_fetcher_error(exc, source=self.name, timeout=timeout) from exc

        status = response.status_code
        # 404/410 是"文章没了"，5xx 和其它 4xx 归到网络/站点侧
        if status in (404, 410):
            raise FetcherError(
                code=FetcherErrorCode.NOT_FOUND, message=f"http {status}", source=self.name
            )
        if status == _RATE_LIMIT_STATUS:
            raise FetcherError(
                code=FetcherErrorCode.RATE_LIMIT, message=f"http {status}", source=self.name
            )
        if status in _AUTH_STATUSES:
            raise FetcherError(
                code=FetcherErrorCode.AUTH, message=f"http {status}", source=self.name
            )
        if status >= 400:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK, message=f"http {status}", source=self.name
            )

        # Content-Type 校验（缺失时按 HTML 宽容处理）
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and content_type not in _HTML_CONTENT_TYPES:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"content-type not html: {content_type!r}",
                source=self.name,
            )

        return response.text, status, str(response.url), content_type


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
