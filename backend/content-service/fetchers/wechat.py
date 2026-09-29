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

微信抓取 4 件套（UA / Referer / Accept / Accept-Language）依据
`docs/2026-09-28_微信公众号文章抓取SOP_v1.0.md`：

- **UA 带 `MicroMessenger/`** —— 与 SOP 实战 UA 对齐。实测同一篇文章用桌面
  Chrome UA 也能拿到 200 + 完整正文，所以 UA 不是成败分水岭；但保持
  MicroMessenger 段是微信生态的正常形态，不亏。
- **Referer / Accept（含 `application/xml`）/ Accept-Language（zh-CN）** ——
  SOP §1.4「任一缺失 = 100% 失败」，照齐。

真正卡死线上抓取的是**反爬判据**：旧 `_check_wechat_block()` 对整页做子串匹配，
而真文章页的 webpack 载荷里天然含"请在微信中打开"11 处，导致 100% 正文页被误判
AUTH。判据已改为「无 `#js_content` + 剔 script 后扫可见文本」双条件。

⚠️ **不要对整页做 `unicode_escape` 解码**（SOP §1.2 的做法）：
公众号正文是 UTF-8 中文，`s.encode('utf-8').decode('unicode_escape')`
会把每个汉字打成 `å¾\x88å¤\x9a` 乱码。实测两篇真实文章：
解码前 `'很多人一听到 FDE…'`，解码后 `'å¾\x88å¤\x9aäººä¸\x80…'`。
而且 `\x3c` / `\u003c` 转义串只出现在页面尾部 webpack JS 载荷里，
**不在 `#js_content` 正文内**（实测正文区间 `\x3c` 计数 = 0），
所以正文抽取根本不需要解码 —— 需要时只定向替换标签相关的少数转义即可。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
# 发布时间兜底：<em id="publish_time"> 常常是空的（实测两篇真实文章均为 ''），
# 真值藏在页面尾部的 JS 变量里 `var ct = "1789965067"`（Unix 秒，UTC+8）。
_CT_TS_RE = re.compile(r'var\s+ct\s*=\s*["\']?(\d{10})["\']?')
_DATA_SRC_RE = re.compile(r'<img[^>]*\bdata-src=["\']([^"\']+)["\']', re.IGNORECASE)
# <title> 的常见后缀："标题 - 公众号名" / "标题 | 公众号名"
_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*[^|\-–—]{1,30}$")

# SOP §3 升级触发：整页 < 10KB 基本是反爬空壳 / 跳转页，不是正文。
# 提前拦下来给"页太小"这个明确结论，避免下游误判成"文章失效"。
MIN_SHELL_BYTES = 10 * 1024
# 真抓取的公众号文章普遍 3.5MB 左右（大量 base64/内联资源），
# 正文容器稳定落在页面 15%~17% 偏移处，所以这条上限留足余量不会切到 #js_content。
MAX_HTML_CHARS = 8 * 1024 * 1024

_WECHAT_BLOCKLIST_PATTERNS: list[tuple[str, FetcherErrorCode, str]] = [
    ("环境异常", FetcherErrorCode.AUTH, "wechat anti-bot environment check"),
    ("请在微信中打开", FetcherErrorCode.AUTH, "wechat requires in-app browser"),
    ("该公众号已迁移", FetcherErrorCode.NOT_FOUND, "wechat account migrated"),
    ("此内容因违规无法查看", FetcherErrorCode.AUTH, "wechat content blocked"),
]

# <script> / <style> 整块：判反爬前先剔掉，否则会扫进 webpack 载荷里的提示文案。
_NONVISUAL_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# 有 #js_content = 这是真文章页，直接放行。
_HAS_JS_CONTENT_RE = re.compile(r'id=["\']js_content["\']', re.IGNORECASE)


def _check_wechat_block(html: str) -> None:
    """命中公众号反爬 / 失效提示页 → 抛 FetcherError（AUTH / NOT_FOUND）。

    ⚠️ 判据必须是**结构 + 可见文本双条件**，不能对整页做子串匹配：

    真文章页（实测 `.../s/ORtvrt9Rg_dgcGdBaPuXyQ`，3.58MB）的 webpack JS 载荷里
    **天然带着**"请在微信中打开"等文案 11 处。整页朴素匹配会把每一篇正常文章
    都判成 AUTH，线上等于 100% 抓取失败。

    所以这里两道闸：
      1. 页面含 `#js_content` → 一定是正文页，直接放行（反爬页没有正文容器）；
      2. 否则先剔掉 `<script>/<style>` 再扫可见文本 —— 实测剔完后真文章里
         这些词出现次数为 0，而真拦截页的提示文案就在 body 可见区里。
    """
    if _HAS_JS_CONTENT_RE.search(html):
        return
    visible = _NONVISUAL_RE.sub(" ", html)
    for pattern, code, msg in _WECHAT_BLOCKLIST_PATTERNS:
        if re.search(pattern, visible):
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

    # 必须带 MicroMessenger 段：少了这一段微信会回"请在微信中打开"壳页（实测）。
    UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.45(0x18002d39) "
        "NetType/WIFI Language/zh_CN"
    )
    TIMEOUT = 30.0
    MAX_HTML_CHARS = MAX_HTML_CHARS  # 8MB：真实公众号文章普遍 3.5MB 左右
    MIN_SHELL_BYTES = MIN_SHELL_BYTES  # 低于 10KB 判定为空壳

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
        return self.parse_article(html, url=url, final_url=final_url, status_code=status_code)

    # -- 下载 ---------------------------------------------------------------
    async def _download(self, url: str, *, timeout: float) -> tuple[str, int, str]:
        """抓 HTML：微信 4 件套齐发，返回 (html, status, final_url)。"""
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            # CP9.x fix：trust_env=False 防止 HTTP_PROXY 拦内网/本机请求
            "trust_env": False,
            "headers": {
                "User-Agent": self.UA,
                "Referer": "https://mp.weixin.qq.com/",
                # application/xml 不能省：缺了部分 CDN 返 406（SOP §1.4）
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
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
            if publish_time is None and raw_local:
                publish_time = _parse_datetime(raw_local.replace(" ", "T"))
                publish_time_raw = raw_local
        # 再兜底 `var ct = "<unix>"`：<em id="publish_time"> 真实文章里常是空的
        # （实测两篇均为 ''），真发布时间只在页面尾部 JS 变量里。
        if publish_time is None:
            ct_match = _CT_TS_RE.search(html)
            if ct_match:
                ts = int(ct_match.group(1))
                publish_time = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
                publish_time_raw = f"var_ct:{ts}"

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
            # SOP §3 升级触发：整页 < 10KB 几乎必是反爬空壳 / 跳转页。
            # 不在前置下载阶段拦（短文/测试 fixture 也可能很小），而是在这里
            # 确认"抽不到正文"之后补上这个结论，让排障方向明确。
            size = len(html.encode("utf-8", errors="ignore"))
            hint = ""
            if size < MIN_SHELL_BYTES:
                hint = f" (page too small: {size}B < {MIN_SHELL_BYTES}B — 反爬空壳/跳转页)"
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"wechat article body (#js_content) not found{hint}",
                source=SOURCE,
            )
        return match.group(1).strip()


__all__ = ["SOURCE", "WECHAT_HOST", "WechatFetcher", "_check_wechat_block"]
