"""抖音视频抓取（v1 §11.2 CP2.3 / CP2.3.1）。

抖音 HTML 不在 DOM 里渲染，数据在 `<script id="RENDER_DATA">` 或 `window._ROUTER_DATA`
的内嵌 JSON 里。短链 `v.douyin.com/iXXXX` 先 302 跳到 `www.douyin.com/video/XXXX` 或
`www.iesdouyin.com/share/video/XXXX`，httpx `follow_redirects=True` 自动做。

策略（CP2.3.1 三路径fallback）：

- 移动端 UA（抖音 PC UA 经常 404 / 反爬；2025-2026 桌面端 JS VM 壳化，RENDER_DATA 拿不到，
  改用移动端 H5 页面 + JSON 接口）
- **主路径**：移动端 H5 页面抽 `window._ROUTER_DATA` → `loaderData.*.videoInfoRes.itemList[0]`
- **Fallback 1**：`www.iesdouyin.com/share/video/{id}/?mid=0` H5 抽 RENDER_DATA / _ROUTER_DATA
- **Fallback 2**：老 `/web/api/v2/aweme/iteminfo/?item_ids={id}` 接口（已知 401/429 限流，概率兜底）
- 兼容路径：仍有部分分享页带桌面端 RENDER_DATA，先 BFS 找 `aweme_detail` 兜底
- 三路全失败 → FetcherError(UNSUPPORTED) → 2001/400（已是"不支持"语义）

失败 → FetcherError：

- 网络错误 / 404 / 5xx → NETWORK / NOT_FOUND
- 找不到 aweme_detail / _ROUTER_DATA → UNSUPPORTED（三路用尽）
- 视频被删除 / 不可见 → NOT_FOUND

本期**没有**代理池 / cookie 池，也不做滑块验证绕过（v1 §11.2 CP2.3 暂不要求登录）。
如果未来反爬加严，扩展点是 DouyinFetcher 的抓取路径（加代理 / cookie），
不是改 fetcher 抽象层（CP2.1 契约定死）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger

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

# 移动端 UA（CP2.3.1）：Android Chrome Mobile，触发移动端 H5 页面 + JSON 接口，
# 避开桌面端 JS VM 壳化（2025-2026 桌面端 RENDER_DATA 已抽不到）。
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

_RENDER_DATA_RE = re.compile(
    r'<script[^>]*id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ROUTER_DATA_RE = re.compile(
    r'window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>', re.DOTALL
)
_ROUTER_DATA_RE_FALLBACK = re.compile(
    r'window\._ROUTER_DATA\s*=\s*(\{.*?\});', re.DOTALL
)
_AWEME_ID_IN_HTML_RE = re.compile(r'"aweme_id"\s*:\s*"?(\d+)"?')

# 抖音"内容不见了"类页面（视频被删 / 私密 / 违规下架）→ NOT_FOUND，不是 PARSE
_DOUYIN_GONE_PATTERNS: list[tuple[str, str]] = [
    ("视频不存在", "douyin video removed"),
    ("内容不存在", "douyin content removed"),
    ("已被删除", "douyin content deleted"),
]

# aweme_id 直接能从 URL 抽出的模式（按优先级，顺序即短路顺序）
_AWEME_ID_URL_PATTERNS: list[str] = [
    r"/video/(\d+)",  # iesdouyin / www.douyin.com/share/video/{id}
    r"modal_id=(\d+)",  # 老链接
    r"/note/(\d+)",  # 图文
    r"aweme_id=(\d+)",  # JSON-LD / URL 参数
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
    """抖音视频/图文抓取（CP2.3 + CP2.3.1 移动端 UA 三路径 fallback）。

    三种入口都走同一条路径：
    - 短链：https://v.douyin.com/i12345（302 跳到真实视频页）
    - 桌面/移动分享页：https://www.douyin.com/video/7123456789012345678
    - 老分享域名：https://www.iesdouyin.com/share/video/7123456789012345678

    抓取顺序：桌面 RENDER_DATA 兜底 → 移动 H5 _ROUTER_DATA → iesdouyin H5
    → 老 iteminfo 接口；全失败 → UNSUPPORTED(2001)。
    """

    UA = MOBILE_UA
    TIMEOUT = 30.0
    FALLBACK_TIMEOUT = 10.0  # 三路径兜底请求超时（独立短超时，避免拖死主流程）
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

        # 兼容路径：部分桌面/分享页仍带 RENDER_DATA（BFS 找 aweme_detail）
        aweme = self._parse_render_data_aweme(html)
        if aweme is not None:
            return self._build_result(
                aweme, url=url, final_url=final_url, status_code=status_code
            )

        # CP2.3.1 三路径 fallback：移动端 H5 → iesdouyin H5 → 老 iteminfo 接口
        aweme_id = await self._extract_aweme_id(final_url)
        if not aweme_id:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message="无法从抖音短链抽取 aweme_id",
                source=self.name,
            )

        client_kwargs: dict[str, Any] = self._client_kwargs()
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            aweme = await self._fetch_mobile_h5(client, aweme_id)
            if aweme is None:
                aweme = await self._fetch_iesdouyin_h5(client, aweme_id)
            if aweme is None:
                aweme = await self._fetch_iesdouyin_api(client, aweme_id)
            if aweme is None:
                raise FetcherError(
                    code=FetcherErrorCode.UNSUPPORTED,
                    message=f"抖音视频抓取全路径失败: {aweme_id}",
                    source=self.name,
                )
            return self._build_result(
                aweme, url=url, final_url=final_url, status_code=status_code
            )

    # -- 下载 ---------------------------------------------------------------
    async def _download(self, url: str, *, timeout: float) -> tuple[str, int, str]:
        """抓 HTML：移动端 UA + follow_redirects（解 v.douyin.com 短链），返回 (html, status, final_url)。"""
        client_kwargs: dict[str, Any] = self._client_kwargs()
        client_kwargs["timeout"] = timeout
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

    def _client_kwargs(self) -> dict[str, Any]:
        """三路径兜底 / 下载共用的 client 参数（移动端 UA + follow_redirects）。"""
        return {
            "timeout": self.FALLBACK_TIMEOUT,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.UA,
                "Referer": "https://www.douyin.com/",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }

    # -- aweme_id 抽取 ------------------------------------------------------
    @staticmethod
    def _extract_aweme_id_from_url(url: str) -> str | None:
        """从 URL 直接抽 aweme_id（短链/分享页/图文/JSON-LD 参数）。

        短链 `v.douyin.com/iXXXX` 没有 id，需跳转后再抽 → 返回 None（由 fetch 走三路径）。
        """
        for pattern in _AWEME_ID_URL_PATTERNS:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        return None

    async def _extract_aweme_id(self, url: str) -> str | None:
        """抽 aweme_id：先看 URL，拿不到再抓页面抽 JSON-LD / _ROUTER_DATA。"""
        aweme_id = self._extract_aweme_id_from_url(url)
        if aweme_id:
            return aweme_id
        # 最后兜底：抓页面抽 aweme_id（短链跳转后的 H5 页面常带）
        try:
            client_kwargs: dict[str, Any] = self._client_kwargs()
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                m = _AWEME_ID_IN_HTML_RE.search(resp.text)
                if m:
                    return m.group(1)
        except Exception as exc:  # 兜底抽取失败不算抓取出错，交给上层判 UNSUPPORTED
            logger.warning("douyin aweme_id fallback extract failed: url=%s err=%s", url, exc)
        return None

    # -- 三路径 fallback ----------------------------------------------------
    async def _fetch_mobile_h5(
        self, client: httpx.AsyncClient, aweme_id: str
    ) -> dict | None:
        """主路径：移动端 H5 页面抽 `window._ROUTER_DATA` → loaderData.*.itemList[0]。"""
        try:
            url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
            resp = await client.get(
                url,
                headers={"Referer": "https://www.douyin.com/"},
                timeout=self.FALLBACK_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            return _router_data_item(resp.text)
        except Exception as exc:
            logger.warning("mobile H5 fetch failed: aweme=%s err=%s", aweme_id, exc)
        return None

    async def _fetch_iesdouyin_h5(
        self, client: httpx.AsyncClient, aweme_id: str
    ) -> dict | None:
        """Fallback 1：iesdouyin H5（?mid=0）抽 RENDER_DATA / _ROUTER_DATA。"""
        try:
            url = f"https://www.iesdouyin.com/share/video/{aweme_id}/?mid=0"
            resp = await client.get(url, timeout=self.FALLBACK_TIMEOUT)
            if resp.status_code != 200:
                return None
            return _router_data_item(resp.text)
        except Exception as exc:
            logger.warning("iesdouyin H5 fetch failed: aweme=%s err=%s", aweme_id, exc)
        return None

    async def _fetch_iesdouyin_api(
        self, client: httpx.AsyncClient, aweme_id: str
    ) -> dict | None:
        """Fallback 2：老 `/web/api/v2/aweme/iteminfo/` 接口（已知 401/429 限流，概率兜底）。"""
        try:
            url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
            resp = await client.get(url, timeout=self.FALLBACK_TIMEOUT)
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data.get("item_list", [])
            return items[0] if items else None
        except Exception as exc:
            logger.warning("iesdouyin API fetch failed: aweme=%s err=%s", aweme_id, exc)
        return None

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
    def _parse_render_data_aweme(html: str) -> dict | None:
        """RENDER_DATA → aweme_detail（拿不到返回 None，交三路径 fallback）。"""
        try:
            data = DouyinFetcher._extract_render_data(html)
        except FetcherError:
            return None
        return _find_aweme_detail(data)

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
                "fetch_method": "douyin_three_path",
            },
        )


def _router_data_item(html: str) -> dict | None:
    """从 HTML 抽 `window._ROUTER_DATA` 第一个 loaderData.*.videoInfoRes.itemList[0]。"""
    m = _ROUTER_DATA_RE.search(html)
    if not m:
        m = _ROUTER_DATA_RE_FALLBACK.search(html)
    if not m:
        return None
    try:
        raw = m.group(1).replace("undefined", "null")
        router_data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    for _key, val in router_data.get("loaderData", {}).items():
        if not isinstance(val, dict):
            continue
        item_list = val.get("videoInfoRes", {}).get("itemList", [])
        if item_list:
            return item_list[0]
    return None


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


__all__ = [
    "DOUYIN_HOSTS",
    "SOURCE",
    "MOBILE_UA",
    "DouyinFetcher",
    "_check_douyin_gone",
    "_find_aweme_detail",
    "_router_data_item",
]
