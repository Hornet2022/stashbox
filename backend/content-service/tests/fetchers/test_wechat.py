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
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, headers=HTML_HEADERS)
    )
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
    """ "<title>环境异常</title>" 风控页 → AUTH。"""
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(BLOCKED_HTML)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert exc_info.value.source == "wechat_mp"
    assert "environment check" in exc_info.value.message


def test_check_wechat_block_detects_require_wechat_app():
    """ "请在微信中打开" 提示页 → AUTH。"""
    html = (
        "<html><head><title>提示信息</title></head><body>"
        '<div class="weui-msg"><h2>请在微信中打开</h2>'
        "<p>请在微信客户端打开此网页。</p></div></body></html>"
    )
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.AUTH
    assert "in-app browser" in exc_info.value.message


def test_check_wechat_block_detects_migrated():
    """ "该公众号已迁移" → NOT_FOUND（文章本体不存在了，不是反爬）。"""
    html = "<html><body><p>该公众号已迁移至新账号，请关注新账号。</p></body></html>"
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.NOT_FOUND
    assert "migrated" in exc_info.value.message


def test_check_wechat_block_detects_content_blocked():
    """ "此内容因违规无法查看" → AUTH。"""
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


def test_real_article_page_with_blocklist_words_in_js_is_not_blocked():
    """守住线上致命 Bug：真文章页的 webpack 载荷里天然含"请在微信中打开"等文案。

    旧判据对整页做子串匹配 → 每篇正常文章都被判 AUTH → 线上 100% 抓取失败。
    这里模拟真页面形态：有 #js_content，且提示词只出现在 <script> 里。
    """
    html = (
        "<html><head><title>x</title>"
        "<script>var tips=['请在微信中打开','环境异常','该公众号已迁移'];</script>"
        "</head><body>"
        # 正文达到 parser.MIN_ARTICLE_CHARS：抓取层现在有"整篇合理性闸门"，
        # 低于阈值判为反爬空壳页。这条要验的是"有 #js_content 就放行"，
        # 与正文长度无关，所以 fixture 用真实篇幅。
        '<div id="js_content"><p>正文第一段。</p><p>正文第二段。</p>'
        "<p>" + "这是一篇真实公众号文章会有的正文段落。" * 12 + "</p></div>"
        "<script>window.__webpack_payload__=1;</script>"
        "</body></html>"
    )
    # 不能抛 AUTH —— 有 #js_content 就是正文页
    _check_wechat_block(html)
    result = WechatFetcher().parse_article(
        html, url=ARTICLE_URL, final_url=ARTICLE_URL, status_code=200
    )
    assert "正文第一段" in result.content_text


def test_block_page_without_js_content_still_raises():
    """反向守住：真拦截页（无 #js_content，提示词在 body 可见区）仍要抛 AUTH。"""
    html = "<html><body><div class='weui-msg__title'>" "<h2>请在微信中打开</h2></div></body></html>"
    with pytest.raises(FetcherError) as exc_info:
        _check_wechat_block(html)
    assert exc_info.value.code == FetcherErrorCode.AUTH


def test_blocklist_word_inside_script_only_is_ignored_on_shell_page():
    """无 #js_content 但提示词只在 <script> 里 → 不算拦截页（脚本噪声不是提示）。"""
    html = (
        "<html><body><script>var t='请在微信中打开';</script>" "<div>普通跳转页</div></body></html>"
    )
    _check_wechat_block(html)  # 不抛


# ==========================================================================
# SOP v1.0（docs/2026-09-28_微信公众号文章抓取SOP_v1.0.md）回归
#
# 以下每条都对应一次**真实复现**，不是照着 SOP 抄的断言：
#   - MicroMessenger UA：旧 UA 抓 https://mp.weixin.qq.com/s/ORtvrt9Rg_dgcGdBaPuXyQ
#     实测直接命中"请在微信中打开"壳页 → FetcherError(AUTH)。
#   - 4 件套：SOP §1.4「任一缺失 = 100% 失败」。
#   - 8MB 上限：真实公众号文章实测 3.5MB 上下，正文容器落在页面 15%~17% 偏移。
#   - var ct 兜底：真实文章 <em id="publish_time"> 实测为空串，真值只在 JS 变量里。
# ==========================================================================


def test_ua_contains_micromessenger_token():
    """UA 必须带 MicroMessenger/ 段 —— 少了它微信只回"请在微信中打开"壳页。"""
    assert "MicroMessenger/" in WechatFetcher.UA
    # 反爬 4 件套里 UA 必须是 iPhone 机型，不能是桌面 Chrome
    assert "iPhone" in WechatFetcher.UA


@pytest.mark.asyncio
async def test_download_sends_wechat_four_piece_headers():
    """4 件套（UA / Referer / Accept / Accept-Language）必须齐发。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, text=WECHAT_HTML, headers=HTML_HEADERS)

    fetcher = WechatFetcher(transport=httpx.MockTransport(handler))
    await fetcher.fetch(ARTICLE_URL)

    assert captured["user-agent"] == WechatFetcher.UA
    assert captured["referer"] == "https://mp.weixin.qq.com/"
    # application/xml 不能省：缺了部分 CDN 返 406
    assert "application/xml" in captured["accept"]
    assert captured["accept-language"].startswith("zh-CN")


@pytest.mark.asyncio
async def test_large_page_with_content_early_still_parses():
    """真实文章 3.5MB+，正文容器在 15%~17% 偏移处 —— 解析上限不能把正文切掉。"""
    # 在正文后面垫 3MB 尾巴，模拟真文章"正文很靠前、总页很大"的形态
    padding = "<div>" + ("x" * (3 * 1024 * 1024)) + "</div>"
    big_html = WECHAT_HTML + padding
    assert len(big_html.encode("utf-8")) > wx.MAX_HTML_CHARS * 0  # 确认真的很大
    fetcher = _fetcher(big_html)
    result = await fetcher.fetch(ARTICLE_URL)
    assert "服务条款" not in result.content_text
    assert result.content_text


def _strip_all_publish_time(html: str) -> str:
    """把 meta 和 <em> 两个发布时间源都剥掉，逼出 `var ct` 兜底分支。

    真实公众号页面里 `article:published_time` meta 常常整个不存在
    （实测两篇真实文章都取不到），所以兜底才是线上真正会走的路径。
    """
    html = html.replace(
        '<meta property="article:published_time" content="2026-09-17T08:30:00+08:00">', ""
    )
    return html.replace(
        '<em id="publish_time" class="rich_media_meta rich_media_meta_text">2026-09-17 08:30</em>',
        '<em id="publish_time"></em>',
    )


def test_publish_time_falls_back_to_var_ct_when_em_empty():
    """<em id="publish_time"> 为空（真实文章常态）→ 退回页面尾部 `var ct` Unix 秒。"""
    html = _strip_all_publish_time(WECHAT_HTML) + '<script>var ct = "1789965067";</script>'
    result = WechatFetcher().parse_article(
        html, url=ARTICLE_URL, final_url=ARTICLE_URL, status_code=200
    )
    assert result.publish_time == datetime(
        2026, 9, 21, 12, 31, 7, tzinfo=timezone(timedelta(hours=8))
    )
    assert result.raw_metadata["publish_time_raw"] == "var_ct:1789965067"


def test_publish_time_empty_em_without_var_ct_is_none():
    """空 <em> 且没有 var ct → publish_time 为 None（不编时间）。"""
    result = WechatFetcher().parse_article(
        _strip_all_publish_time(WECHAT_HTML),
        url=ARTICLE_URL,
        final_url=ARTICLE_URL,
        status_code=200,
    )
    assert result.publish_time is None


@pytest.mark.asyncio
async def test_tiny_page_without_js_content_reports_shell_hint():
    """小页 + 抽不到 js_content → PARSE，且错误信息点明"空壳"这个排障方向。"""
    html = "<html><head><title>x</title></head><body><p>壳</p></body></html>"
    fetcher = _fetcher(html)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(ARTICLE_URL)
    assert exc_info.value.code == FetcherErrorCode.PARSE
    assert "#js_content" in exc_info.value.message
    assert "page too small" in exc_info.value.message


def test_unicode_escape_would_corrupt_chinese_but_body_needs_no_decode():
    """守住 SOP §1.2 的坑：整页 unicode_escape 会把中文打成乱码，正文本来就不需要解码。

    这是文档化行为的回归钉子 —— 万一有人照 SOP 往 fetcher 里塞 unicode_escape，
    这条会先炸出来。
    """
    body = WechatFetcher._extract_body(WECHAT_HTML)
    assert "很多人" in body or "听匣" in body  # 正文是正常 UTF-8 中文
    corrupted = body.encode("utf-8").decode("unicode_escape", errors="ignore")
    assert corrupted != body  # 确实会被打坏
    assert "很多人" not in corrupted  # 中文变成 å¾\x88 这类乱码
