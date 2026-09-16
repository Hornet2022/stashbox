"""GenericURLFetcher 单测 + 集成测（v1 §11.2 CP2.4）。

导入说明同 test_base.py：content-service 目录带连字符 + 本测试包自己也叫 `fetchers`，
所以用 importlib 以别名 `cs_fetchers_gu` 加载被测包，避免 `import fetchers` 命中自己。

集成测全部走 httpx.MockTransport，**不发真实网络请求**。
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "article.html").read_text(encoding="utf-8")

if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load_fetchers():
    pkg_dir = CONTENT_SERVICE_DIR / "fetchers"
    spec = importlib.util.spec_from_file_location(
        "cs_fetchers_gu",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cs_fetchers_gu"] = module
    spec.loader.exec_module(module)
    return module


cs = _load_fetchers()
gu = sys.modules["cs_fetchers_gu.generic_url"]  # 5 个 HTMLParser 在这里

_TitleExtractor = gu._TitleExtractor
_AuthorExtractor = gu._AuthorExtractor
_TimeExtractor = gu._TimeExtractor
_MediaExtractor = gu._MediaExtractor
_ContentExtractor = gu._ContentExtractor
GenericURLFetcher = cs.GenericURLFetcher
FetcherError = cs.FetcherError
FetcherErrorCode = cs.FetcherErrorCode

ARTICLE_URL = "https://example.com/2026/09/01/why-stashbox"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def _fetcher(handler) -> GenericURLFetcher:
    """用 MockTransport 造一个不发真请求的 fetcher。"""
    return GenericURLFetcher(transport=httpx.MockTransport(handler))


def _html_response(body: str = FIXTURE_HTML, **kwargs) -> httpx.Response:
    return httpx.Response(200, text=body, headers=HTML_HEADERS, **kwargs)


# ==========================================================================
# 6.1 单元测试：5 个 HTMLParser
# ==========================================================================


def test_title_extractor_basic():
    """只有 <title> 时用 <title>；空白折叠掉。"""
    html = "<html><head><title>\n  普通标题  \n</title></head><body></body></html>"
    extractor = _TitleExtractor().parse(html)
    assert extractor.title.strip() == "普通标题"
    assert extractor.og_title == ""
    assert extractor.best == "普通标题"


def test_title_extractor_with_og_title():
    """og:title 优先于 <title>。"""
    html = (
        '<html><head><title>浏览器标签标题</title>'
        '<meta property="og:title" content="社交分享用的标题">'
        "</head><body></body></html>"
    )
    extractor = _TitleExtractor().parse(html)
    assert extractor.title == "浏览器标签标题"
    assert extractor.og_title == "社交分享用的标题"
    assert extractor.best == "社交分享用的标题"

    # 没有 og:title 时退回 <title>
    assert _TitleExtractor().parse("<title>只有 title</title>").best == "只有 title"


def test_author_extractor_multiple_sources():
    """author → article:author → twitter:creator 顺序取第一个非空值。"""
    html = (
        '<meta name="author" content="林承宇">'
        '<meta property="article:author" content="第二作者">'
        '<meta name="twitter:creator" content="@third">'
    )
    assert _AuthorExtractor().parse(html).best == "林承宇"

    # 缺 author 时走 article:author
    assert (
        _AuthorExtractor().parse(
            '<meta property="article:author" content="第二作者">'
            '<meta name="twitter:creator" content="@third">'
        ).best
        == "第二作者"
    )
    # 只剩 twitter:creator
    assert _AuthorExtractor().parse('<meta name="twitter:creator" content="@third">').best == "@third"
    # 一个都没有 → None（不是空串，FetchResult.author 默认 None）
    assert _AuthorExtractor().parse("<p>没有作者</p>").best is None


def test_time_extractor_iso8601_and_rfc822():
    """ISO 8601 / RFC 822 / <time datetime> 三种来源都能解析，无时区按 UTC。"""
    iso = _TimeExtractor().parse(
        '<meta property="article:published_time" content="2026-09-01T08:30:00+08:00">'
    )
    assert iso.best == datetime(2026, 9, 1, 8, 30, tzinfo=timezone(timedelta(hours=8)))

    rfc822 = _TimeExtractor().parse('<meta name="pubdate" content="Tue, 01 Sep 2026 08:30:00 GMT">')
    assert rfc822.best == datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)

    # <time datetime> 兜底；Z 结尾按 UTC
    assert _TimeExtractor().parse('<time datetime="2026-09-01T08:30:00Z">9月1日</time>').best == (
        datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)
    )
    # 无时间信息 → None
    assert _TimeExtractor().parse("<p>没有时间</p>").best is None
    # 无时区的 ISO 串按 UTC 处理（FetchResult 要求带时区）
    assert _TimeExtractor().parse('<meta name="pubdate" content="2026-09-01 08:30:00">').best == (
        datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)
    )


def test_media_extractor_with_relative_urls():
    """相对路径用 urljoin 转绝对；去重且保持文档顺序。"""
    html = (
        '<meta property="og:image" content="https://cdn.example.com/og.jpg">'
        '<img src="/static/x.png">'
        '<img src="https://cdn.example.com/og.jpg">'  # 重复，应去重
        '<meta property="og:video" content="/media/v.mp4">'
    )
    urls = _MediaExtractor(base_url="https://example.com/a/b").parse(html).urls
    assert urls == [
        "https://cdn.example.com/og.jpg",
        "https://example.com/static/x.png",
        "https://example.com/media/v.mp4",
    ]
    # data: / javascript: 伪协议不收
    assert _MediaExtractor(base_url="https://example.com").parse('<img src="data:image/png;base64,AA">').urls == []


def test_content_extractor_density_ranking():
    """高密度正文段落胜出，低密度导航段落被丢掉。"""
    high = "<p>" + "这是一段真正的正文内容，没有塞满链接，所以文本密度很高。" * 3 + "</p>"
    low = "<p>" + '<a href="/tag/%d">导航词</a><span>·</span>' % 1 + "</p>"
    html = f"<article>{low}{high}</article>"
    extractor = _ContentExtractor().parse(html)

    assert len(extractor.blocks) == 1  # 低密度段被 MIN_TEXT_DENSITY 过滤
    assert extractor.blocks[0]["density"] >= gu.MIN_TEXT_DENSITY
    assert "这是一段真正的正文内容" in extractor.content_text()
    assert "导航词" not in extractor.content_text()
    assert extractor.content_html().startswith("<p>")

    # 容器段（<article> 包着 <p>）不参与竞争，只输出叶子段
    assert all(b["tag"] == "p" for b in extractor.blocks)


# ==========================================================================
# 6.2 集成测试：httpx.MockTransport，不发真请求
# ==========================================================================


@pytest.mark.asyncio
async def test_fetch_happy_path():
    """正常 HTML → FetchResult 全字段填充。"""
    fetcher = _fetcher(lambda request: _html_response())
    result = await fetcher.fetch(ARTICLE_URL)

    assert result.title == "听匣专栏：为什么我们需要一个「稍后听」的盒子"  # og:title 优先
    assert result.author == "林承宇"
    assert result.publish_time is not None
    assert result.publish_time.isoformat() == "2026-09-01T08:30:00+08:00"
    assert result.media_urls == [
        "https://cdn.example.com/og-cover.jpg",
        "https://cdn.example.com/fig-cover.png",
        "https://example.com/static/fig1.png",
        "https://example.com/static/fig2.png",
    ]
    assert result.source == "generic_url"
    assert result.url == ARTICLE_URL

    # 正文：5 段高密度段落按文档顺序拼接
    assert result.content_text.startswith("每天早上打开手机")
    assert "最后一层是取舍" in result.content_text
    assert result.content_html.count("<p>") == 5
    # 侧栏导航 / 页脚 / <script> 里的文字都不该出现
    assert "今日榜" not in result.content_text
    assert "服务条款" not in result.content_text
    assert "SCRIPT_MARKER" not in result.content_text
    assert result.raw_metadata["paragraph_count"] == 5
    assert result.raw_metadata["status_code"] == 200


@pytest.mark.asyncio
async def test_fetch_404_raises_not_found():
    """404 / 410 → NOT_FOUND。"""
    for status in (404, 410):
        fetcher = _fetcher(lambda request, s=status: httpx.Response(s, text="gone"))
        with pytest.raises(FetcherError) as exc_info:
            await fetcher.fetch(ARTICLE_URL)
        assert exc_info.value.code == FetcherErrorCode.NOT_FOUND
        assert exc_info.value.source == "generic_url"


@pytest.mark.asyncio
async def test_fetch_5xx_raises_network():
    """5xx → NETWORK（不是 PARSE，站点侧问题）。"""
    fetcher = _fetcher(lambda request: httpx.Response(503, text="unavailable"))
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.NETWORK
    assert "503" in exc_info.value.message


@pytest.mark.asyncio
async def test_fetch_non_html_content_type():
    """content-type 不是 html → PARSE。"""
    handler = lambda request: httpx.Response(  # noqa: E731
        200, json={"ok": True}, headers={"content-type": "application/json"}
    )
    fetcher = _fetcher(handler)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.PARSE
    assert "application/json" in exc_info.value.message


@pytest.mark.asyncio
async def test_fetch_handles_redirect():
    """302 → httpx follow_redirects 跟到最终页，相对 URL 按最终 URL 解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "https://other.example/new"})
        return _html_response()

    fetcher = _fetcher(handler)
    result = await fetcher.fetch("https://example.com/old")

    assert result.raw_metadata["final_url"] == "https://other.example/new"
    assert result.raw_metadata["status_code"] == 200
    # 相对路径按 redirect 之后的 host 解析
    assert "https://other.example/static/fig1.png" in result.media_urls


@pytest.mark.asyncio
async def test_fetch_handles_relative_urls_in_html():
    """<img src="/static/x.png"> → media_urls 里是绝对 URL。"""
    html = (
        '<html><head><title>相对路径测试</title></head><body>'
        '<p>' + "正文内容必须足够长才能通过密度阈值，否则会被当成导航段落丢掉。" * 2 + '</p>'
        '<img src="/static/x.png"><img src="../up/y.png">'
        "</body></html>"
    )
    fetcher = _fetcher(lambda request: _html_response(html))
    result = await fetcher.fetch("https://example.com/deep/dir/page")

    assert result.title == "相对路径测试"
    assert result.media_urls == [
        "https://example.com/static/x.png",
        "https://example.com/deep/up/y.png",  # ../ 相对 /deep/dir/page 的目录
    ]
