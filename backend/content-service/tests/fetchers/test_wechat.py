"""WechatFetcher 单测 + 集成测（v1 §11.2 CP2.2）。

导入说明同 test_generic_url.py：content-service 目录带连字符 + 本测试包自己也叫 `fetchers`，
所以用 importlib 以别名 `cs_fetchers_wx` 加载被测包，避免 `import fetchers` 命中自己。

全部走 httpx.MockTransport，**不发真实网络请求**（公众号反爬，测试更不能真打）。
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
FIXTURES = Path(__file__).parent / "fixtures"
WECHAT_HTML = (FIXTURES / "wechat_article.html").read_text(encoding="utf-8")
BLOCKED_HTML = (FIXTURES / "wechat_blocked.html").read_text(encoding="utf-8")
CP24_HTML = (FIXTURES / "article.html").read_text(encoding="utf-8")  # CP2.4 通用页 fixture

if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load_fetchers():
    pkg_dir = CONTENT_SERVICE_DIR / "fetchers"
    spec = importlib.util.spec_from_file_location(
        "cs_fetchers_wx",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cs_fetchers_wx"] = module
    spec.loader.exec_module(module)
    return module


cs = _load_fetchers()
parser = sys.modules["cs_fetchers_wx.parser"]  # CP2.2 抽出来的 5 个解析器
gu = sys.modules["cs_fetchers_wx.generic_url"]  # CP2.4 的 fetcher（验证平移后行为一致）
wx = sys.modules["cs_fetchers_wx.wechat"]

WechatFetcher = cs.WechatFetcher
FetcherError = cs.FetcherError
FetcherErrorCode = cs.FetcherErrorCode
_check_wechat_block = wx._check_wechat_block

ARTICLE_URL = "https://mp.weixin.qq.com/s/XyZ123AbC789"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def _fetcher(body: str = WECHAT_HTML) -> WechatFetcher:
    """用 MockTransport 造一个不发真请求的 fetcher。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, headers=HTML_HEADERS))
    return WechatFetcher(transport=transport)


# ==========================================================================
# 5.1 parser 复用验证：抽到 parser.py 的 5 个 _Extractor 行为不变
# ==========================================================================


def test_parser_extractors_match_cp24_behavior():
    """CP2.4 的 article.html fixture 喂给 parser.py 的 5 个解析器，结果必须和 CP2.4 一致。"""
    # 平移是纯搬家：generic_url 里的旧符号就是 parser.py 的同一个类
    assert gu._TitleExtractor is parser.TitleExtractor
    assert gu._AuthorExtractor is parser.AuthorExtractor
    assert gu._TimeExtractor is parser.TimeExtractor
    assert gu._MediaExtractor is parser.MediaExtractor
    assert gu._ContentExtractor is parser.ContentExtractor

    title_ex = parser.TitleExtractor().parse(CP24_HTML)
    author_ex = parser.AuthorExtractor().parse(CP24_HTML)
    time_ex = parser.TimeExtractor().parse(CP24_HTML)
    media_ex = parser.MediaExtractor(base_url="https://example.com/2026/09/01/x").parse(CP24_HTML)
    content_ex = parser.ContentExtractor().parse(CP24_HTML)

    assert title_ex.best == "听匣专栏：为什么我们需要一个「稍后听」的盒子"  # og:title 优先
    assert title_ex.og_title == "听匣专栏：为什么我们需要一个「稍后听」的盒子"
    assert title_ex.title.strip() == "为什么我们需要一个「稍后听」的盒子"
    assert author_ex.best == "林承宇"
    assert time_ex.best == datetime(2026, 9, 1, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    assert media_ex.urls == [
        "https://cdn.example.com/og-cover.jpg",
        "https://cdn.example.com/fig-cover.png",
        "https://example.com/static/fig1.png",
        "https://example.com/static/fig2.png",
    ]

    # 正文：密度启发式阈值未动 → 仍是 5 段、仍是同一批段落
    blocks = content_ex.best()
    assert len(blocks) == 5
    assert all(b["density"] >= parser.MIN_TEXT_DENSITY for b in blocks)
    assert content_ex.content_text().startswith("每天早上打开手机")
    assert "最后一层是取舍" in content_ex.content_text()
    assert content_ex.content_html().count("<p>") == 5
    # <script> / 侧栏 / 页脚依旧被排除
    assert "SCRIPT_MARKER" not in content_ex.content_text()
    assert "服务条款" not in content_ex.content_text()

    # CP2.4 fetcher 全链路也没退化（同一个 MockTransport fixture）
    fetcher = gu.GenericURLFetcher(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=CP24_HTML, headers=HTML_HEADERS)
        )
    )
    import asyncio

    result = asyncio.run(fetcher.fetch("https://example.com/2026/09/01/why-stashbox"))
    assert result.raw_metadata["paragraph_count"] == 5
    assert result.source == "generic_url"


# ==========================================================================
# 5.2 反爬检测
# ==========================================================================


def test_check_wechat_block_detects_environment_check():
    """"<title>环境异常</title>" 风控页 → AUTH。"""
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(BLOCKED_HTML)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert exc_info.value.source == "wechat_mp"
    assert "environment check" in exc_info.value.message


def test_check_wechat_block_detects_require_wechat_app():
    """"请在微信中打开" 提示页 → AUTH。"""
    html = (
        '<html><head><title>提示信息</title></head><body>'
        '<div class="weui-msg"><h2>请在微信中打开</h2>'
        "<p>请在微信客户端打开此网页。</p></div></body></html>"
    )
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert "in-app browser" in exc_info.value.message


def test_check_wechat_block_detects_migrated():
    """"该公众号已迁移" → NOT_FOUND（文章本体不存在了，不是反爬）。"""
    html = "<html><body><p>该公众号已迁移至新账号，请关注新账号。</p></body></html>"
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.NOT_FOUND
    assert "migrated" in exc_info.value.message


def test_check_wechat_block_detects_content_blocked():
    """"此内容因违规无法查看" → AUTH。"""
    html = "<html><body><p>此内容因违规无法查看</p></body></html>"
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert "blocked" in exc_info.value.message


def test_check_wechat_block_passes_normal_html():
    """正常文章页不抛（正文里没踩到 4 个黑名单关键词）。"""
    _check_wechat_block(WECHAT_HTML)  # 不抛即通过
    _check_wechat_block(CP24_HTML)  # 通用页也不该被误伤


# ==========================================================================
# 5.3 E2E：httpx.MockTransport，不发真请求
# ==========================================================================


@pytest.mark.asyncio
async def test_wechat_fetch_happy_path():
    """MockTransport 返回公众号文章 fixture → FetchResult 全字段 + #js_content 正文。"""
    fetcher = _fetcher()
    result = await fetcher.fetch(ARTICLE_URL)

    assert result.url == ARTICLE_URL
    assert result.source == "wechat_mp"
    # <title> 是 "标题 - 公众号名"，后缀已剥掉（公众号名来自 <a id="js_name">）
    assert result.title == "为什么我们需要一个「稍后听」的盒子"
    assert result.author == "林承宇"  # <meta name="author"> 优先
    assert result.publish_time is not None
    assert result.publish_time.isoformat() == "2026-09-17T08:30:00+08:00"

    # media：og:image + 正文里懒加载的 <img data-src>（公众号真图）
    assert result.media_urls == [
        "https://mmbiz.qpic.cn/mmbiz_jpg/cover-screenshot.jpg",
        "https://mmbiz.qpic.cn/mmbiz_jpg/body-scene01.jpg?wx_fmt=jpeg",
        "https://mmbiz.qpic.cn/mmbiz_png/body-scene02.png?wx_fmt=png",
    ]

    # 正文：只取 #js_content 内部
    assert result.content_text.startswith("每天早上打开手机")
    assert "不做第二个阅读器" in result.content_text  # blockquote 也算正文
    assert result.content_html.count("<p>") == 8
    # js_content 之外的导航栏 / 推荐位 / 页脚 / <script> 都不该进来
    assert "推荐阅读" not in result.content_text
    assert "服务条款" not in result.content_text
    assert "第 42 期" not in result.content_text
    assert "SCRIPT_MARKER" not in result.content_text

    assert result.raw_metadata["wechat_id"] == "听匣专栏"
    assert result.raw_metadata["status_code"] == 200


@pytest.mark.asyncio
async def test_wechat_fetch_blocked_raises_auth():
    """fixture 是"环境异常"反爬页 → FetcherError(AUTH, source='wechat_mp')，且拿不到正文。"""
    fetcher = _fetcher(BLOCKED_HTML)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert exc_info.value.source == "wechat_mp"


@pytest.mark.asyncio
async def test_wechat_fetch_missing_js_content_raises_parse():
    """页面既不是反爬页也拿不到 #js_content（结构变了 / 假页面）→ PARSE。"""
    html = (
        "<html><head><title>不是文章页</title></head><body>"
        '<div class="rich_media_content"><p>没有 id=js_content 的容器</p></div>'
        "</body></html>"
    )
    fetcher = _fetcher(html)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.PARSE
    assert "#js_content" in exc_info.value.message


@pytest.mark.asyncio
async def test_wechat_fetch_http_404_raises_not_found():
    """404 → NOT_FOUND（网络层错误码与 CP2.4 对齐）。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="gone"))
    fetcher = WechatFetcher(transport=transport)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.NOT_FOUND
    assert exc_info.value.source == "wechat_mp"
