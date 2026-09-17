"""DouyinFetcher 单测（v1 §11.2 CP2.3）。

导入说明同 test_wechat.py：content-service 目录带连字符 + 本测试包自己也叫 `fetchers`，
所以用 importlib 以别名 `cs_fetchers_dy` 加载被测包，避免 `import fetchers` 命中自己。

全部走 httpx.MockTransport，**不发真实网络请求**（抖音反爬 + 限流，测试更不能真打）。
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
FIXTURES = Path(__file__).parent / "fixtures"
DOUYIN_HTML = (FIXTURES / "douyin_video.html").read_text(encoding="utf-8")

if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load_fetchers():
    pkg_dir = CONTENT_SERVICE_DIR / "fetchers"
    spec = importlib.util.spec_from_file_location(
        "cs_fetchers_dy",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cs_fetchers_dy"] = module
    spec.loader.exec_module(module)
    return module


cs = _load_fetchers()
dy = sys.modules["cs_fetchers_dy.douyin"]

DouyinFetcher = cs.DouyinFetcher
FetcherError = cs.FetcherError
FetcherErrorCode = cs.FetcherErrorCode
_find_aweme_detail = dy._find_aweme_detail

SHORT_URL = "https://v.douyin.com/i12345"
VIDEO_URL = "https://www.douyin.com/video/7123456789012345678"
REAL_URL = "https://www.iesdouyin.com/share/video/7123456789012345678"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}
CREATE_TIME = 1758000000  # 2025-09-16T05:20:00Z


def _fetcher(body: str = DOUYIN_HTML) -> DouyinFetcher:
    """用 MockTransport 造一个不发真请求的 fetcher。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, headers=HTML_HEADERS)
    )
    return DouyinFetcher(transport=transport)


# ==========================================================================
# 5.1 短链解析：v.douyin.com/i12345 → 302 → iesdouyin 真实页
# ==========================================================================


@pytest.mark.asyncio
async def test_short_link_follow_redirect():
    """短链 302 跳 www.iesdouyin.com/share/video/xxxx → final_url 是跳转后的真实 URL。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"Location": REAL_URL})
        return httpx.Response(200, text=DOUYIN_HTML, headers=HTML_HEADERS)

    fetcher = DouyinFetcher(transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(SHORT_URL)

    assert result.url == SHORT_URL  # 原始 URL 是短链
    assert result.raw_metadata["final_url"] == REAL_URL  # 302 跳完落在这
    assert result.source == "douyin"
    assert result.title == "这是抖音视频描述 #话题#"


# ==========================================================================
# 5.2 RENDER_DATA 抽取（BFS 找 aweme_detail）
# ==========================================================================


def test_find_aweme_detail_bfs_top_level():
    """aweme_detail 在顶层 dict（当前抖音页面结构）。"""
    data = {
        "anchor": {},
        "aweme_detail": {"aweme_id": "71234567890123456789", "desc": "顶层"},
    }
    assert _find_aweme_detail(data) == data["aweme_detail"]
    assert _find_aweme_detail({"anchor": {}, "app": {"page": "video"}}) is None


def test_find_aweme_detail_bfs_nested():
    """aweme_detail 藏在嵌套 dict / list 里（兼容抖音新版页面多包一层）。"""
    data = {
        "app": {
            "videoInfo": {
                "reserve": [{"aweme_detail": {"aweme_id": "nested-1"}}],
            }
        }
    }
    assert _find_aweme_detail(data)["aweme_id"] == "nested-1"

    deep = {"a": {"b": [{"c": {"aweme_detail": {"aweme_id": "deep-2"}}}]}}
    assert _find_aweme_detail(deep)["aweme_id"] == "deep-2"


# ==========================================================================
# 5.3 反爬 / 错误页
# ==========================================================================


@pytest.mark.asyncio
async def test_douyin_fetch_404_raises_not_found():
    """status=404 → FetcherError(NOT_FOUND, source='douyin')。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="gone"))
    fetcher = DouyinFetcher(transport=transport)
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch(VIDEO_URL)
    assert exc_info.value.code == FetcherErrorCode.NOT_FOUND
    assert exc_info.value.source == "douyin"


# ==========================================================================
# 5.4 E2E：MockTransport 返回完整抖音 HTML（含 RENDER_DATA）
# ==========================================================================


@pytest.mark.asyncio
async def test_douyin_fetch_happy_path():
    """RENDER_DATA 里的 aweme_detail → FetchResult 全字段。"""
    fetcher = _fetcher()
    result = await fetcher.fetch(VIDEO_URL)

    assert result.url == VIDEO_URL
    assert result.source == "douyin"
    assert result.title == "这是抖音视频描述 #话题#"  # title = desc（话题 tag 已在 desc 里）
    assert result.author == "测试作者"  # author.nickname
    assert result.content_text == "这是抖音视频描述 #话题#"

    # media_urls = [cover, play_addr]（封面在前，视频直链在后）
    assert result.media_urls == [
        "https://example.com/cover.jpg",
        "https://example.com/video.mp4",
    ]

    # publish_time：create_time 是 Unix 秒，按 UTC 转 datetime（必须带时区）
    assert result.publish_time == datetime.fromtimestamp(CREATE_TIME, tz=timezone.utc)
    assert result.publish_time.tzinfo is not None

    assert result.raw_metadata["aweme_id"] == "71234567890123456789"
    assert result.raw_metadata["duration_ms"] == 30000
    assert result.raw_metadata["status_code"] == 200
    assert result.raw_metadata["final_url"] == VIDEO_URL
