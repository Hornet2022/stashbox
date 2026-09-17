"""抖音视频抓取（v1 §11.2 CP2.3）。

抖音 HTML 不在 DOM 里渲染，数据在 `<script id="RENDER_DATA">` 的内嵌 JSON 里。
短链 `v.douyin.com/iXXXX` 先 302 跳到 `www.douyin.com/video/XXXX` 或
`www.iesdouyin.com/share/video/XXXX`，httpx `follow_redirects=True` 自动做。

策略：

- 移动端 UA（抖音 PC UA 经常 404 / 反爬）
- 不解析 JS（只抽 RENDER_DATA）
- `aweme_detail` BFS 找一次（兼容嵌套层级变化）

失败 → FetcherError：

- 网络错误 / 404 / 5xx → NETWORK / NOT_FOUND
- 找不到 `<script id="RENDER_DATA">` → PARSE
- RENDER_DATA 解码失败 / JSON 格式错 → PARSE
- 找不到 aweme_detail → PARSE
- 视频被删除 / 不可见 → NOT_FOUND

本期**没有**代理池 / cookie 池，也不做滑块验证绕过（v1 §11.2 CP2.3 暂不要求登录）。
如果未来反爬加严，扩展点是 `DouyinFetcher._download()`（加代理 / cookie），
不是改 fetcher 抽象层（CP2.1 契约定死）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .base import (
    FetchResult,
    Fetcher,
    FetcherError,
    FetcherErrorCode,
)
from .parser import _norm

# 抖音的三种常见域名：
# - www.douyin.com     分享出来的视频/图文页
# - v.douyin.com       短链（302 到 www.douyin.com / www.iesdouyin.com）
# - www.iesdouyin.com  老分享域名
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
SOURCE = "douyin"

_RENDER_DATA_RE = re.compile(
    r'<script[^>]*id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# 抖音"内容不见了"类页面（视频被删 / 私密 / 违规下架）→ NOT_FOUND，不是 PARSE
_DOUYIN_GONE_PATTERNS: list[tuple[str, str]] = [
    ("视频不存在", "douyin video removed"),
    ("内容不存在", "douyin content removed"),
    ("已被删除", "douyin content deleted"),
]


def _find_aweme_detail(data: Any) -> dict | None:
    """BFS 找 aweme_detail 节点（兼容嵌套层级变化）。"""
    queue = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if "aweme_detail" in node and isinstance(node["aweme_detail"], dict):
                return node["aweme_detail"]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def _check_douyin_gone(html: str) -> None:
    """命中"内容不见了"页面 → 抛 FetcherError(NOT_FOUND)。"""
    for pattern, msg in _DOUYIN_GONE_PATTERNS:
        if pattern in html:
            raise FetcherError(code=FetcherErrorCode.NOT_FOUND, message=msg, source=SOURCE)


class DouyinFetcher(Fetcher):
    """抖音视频/图文抓取（CP2.3 实现）。

    三种入口都走同一条路径：
    - 短链：https://v.douyin.com/i12345（302 跳到真实视频页）
    - 桌面/移动分享页：https://www.douyin.com/video/7123456789012345678
    - 老分享域名：https://www.iesdouyin.com/share/video/7123456789012345678
    """

    UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1 DouyinFetcher/0.1"
    )
    TIMEOUT = 30.0
    MAX_HTML_CHARS = 2 * 1024 * 1024  # 解析前 HTML 截断（对齐 wechat / generic_url 的防爆上限）

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 只是测试注入点（httpx.MockTransport），生产走默认 transport
        self._transport = transport

    @property
    def name(self) -> str:
        return SOURCE

    def supports(self, url: str) -> bool:
        return any(host in url for host in DOUYIN_HOSTS)

    async def fetch(self, url: str, *, timeout: float = TIMEOUT) -> FetchResult:
        # 非抖音 URL 不发请求（CP2.1 契约：supports() 说了算），否则会被厂商当爬虫
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
        _check_douyin_gone(html)
        data = self._extract_render_data(html)
        aweme = _find_aweme_detail(data)
        if aweme is None:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="aweme_detail not found in RENDER_DATA",
                source=self.name,
            )
        return self._build_result(
            aweme, url=url, final_url=final_url, status_code=status_code
        )

    # -- 下载 ---------------------------------------------------------------
    async def _download(self, url: str, *, timeout: float) -> tuple[str, int, str]:
        """抓 HTML：移动端 UA + follow_redirects（解 v.douyin.com 短链），返回 (html, status, final_url)。"""
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.UA,
                "Referer": "https://www.douyin.com/",
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
    @staticmethod
    def _extract_render_data(html: str) -> Any:
        """抽 `<script id="RENDER_DATA">` 里的 URL-encoded JSON → dict。"""
        match = _RENDER_DATA_RE.search(html)
        if not match:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="RENDER_DATA script not found",
                source=SOURCE,
            )
        raw = match.group(1).strip()
        try:
            return json.loads(unquote(raw))
        except (ValueError, TypeError) as exc:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"RENDER_DATA decode failed: {type(exc).__name__}: {exc}",
                source=SOURCE,
            ) from exc

    @staticmethod
    def _build_result(
        aweme: dict, *, url: str, final_url: str, status_code: int
    ) -> FetchResult:
        """aweme_detail dict → FetchResult（测试和真抓取共用同一条解析路径）。"""
        desc = _norm(str(aweme.get("desc") or ""))
        author = aweme.get("author") or {}
        nickname = _norm(str(author.get("nickname") or "")) or None
        video = aweme.get("video") or {}
        play_addr = video.get("play_addr") or {}
        cover = video.get("cover") or {}

        media_urls = [
            u
            for u in (
                _first_url(cover.get("url_list")),
                _first_url(play_addr.get("url_list")),
            )
            if u
        ]

        create_time = aweme.get("create_time")
        publish_time = _to_datetime(create_time)

        return FetchResult(
            url=url,
            title=desc or str(aweme.get("aweme_id") or "") or final_url,
            content_html="",  # 抖音没有服务端渲染的正文 HTML，正文只有 desc
            content_text=desc,
            author=nickname,
            publish_time=publish_time,
            media_urls=media_urls,
            source=SOURCE,
            raw_metadata={
                "aweme_id": aweme.get("aweme_id"),
                "duration_ms": video.get("duration"),
                "author_uid": author.get("uid"),
                "final_url": final_url,
                "status_code": status_code,
                "publish_time_raw": create_time,
            },
        )


def _first_url(url_list: Any) -> str | None:
    """`url_list` 可能缺失 / 为空 / 不是 list —— 取第一个非空字符串。"""
    if isinstance(url_list, list):
        for item in url_list:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _to_datetime(create_time: Any) -> datetime | None:
    """抖音 create_time 是 Unix 秒（int / 数字字符串），无时区按 UTC。"""
    try:
        ts = int(create_time)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


__all__ = ["DOUYIN_HOSTS", "SOURCE", "DouyinFetcher", "_check_douyin_gone", "_find_aweme_detail"]
