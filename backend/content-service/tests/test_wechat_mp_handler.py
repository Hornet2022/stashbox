"""CP2.5 服务号 Handler 单测（11 个 case）。

覆盖：URL 解析 2 + URL 合法性 3 + FetcherError→BizException 映射 5 + 端点 E2E 1。

导入说明：content-service 目录名带连字符，不能 import，只能按文件加载
（做法同 backend/tests/content/helpers.py）。main.py 自己会 sys.path.insert
自身目录，加载后 `sys.modules["fetchers"]` 就是被测的 fetchers 包 —— 端点
里 `get_fetcher()` 引用的正是它，所以 E2E 替换它的 `_ALL_FETCHERS` 即可。

前置：本机 PG 5432 + Redis 6379 已起（仅 E2E 用到，建 article）。
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[1]  # backend/content-service/
# `stashbox` 包从仓库父目录解析（本地跑时 pytest 不一定带 PYTHONPATH）
REPO_PARENT = str(CONTENT_SERVICE_DIR.parent.parent.parent)
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)


def _load_module(name: str, path: Path, submodule_search_locations=None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=submodule_search_locations
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 本测试文件不在包里，pytest 会把 tests/ 塞进 sys.path —— 而 tests/fetchers/ 也叫
# `fetchers`，整目录跑时会先被 import，把真正的 fetchers 包顶掉。故先把真包以别名
# 加载并临时挂到 "fetchers" 名下，main.py 加载完再还原（同名遮蔽同理见 test_base.py）。
cs_fetchers = _load_module(
    "_cp25_fetchers_pkg",
    CONTENT_SERVICE_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_SERVICE_DIR / "fetchers")],
)
_shadowed = sys.modules.get("fetchers")
sys.modules["fetchers"] = cs_fetchers
try:
    content_main = _load_module("_cp25_content_main", CONTENT_SERVICE_DIR / "main.py")
finally:
    if _shadowed is None:
        del sys.modules["fetchers"]
    else:
        sys.modules["fetchers"] = _shadowed

_extract_url = content_main._extract_url
_is_valid_url = content_main._is_valid_url
map_fetcher_error = content_main.map_fetcher_error
FetcherError = cs_fetchers.FetcherError
FetcherErrorCode = cs_fetchers.FetcherErrorCode

from stashbox.backend.common.database import AsyncSessionLocal  # noqa: E402
from stashbox.backend.common.models import Article  # noqa: E402

MP_URL = "/api/v1/callback/wechat-mp-message"


class FakeAIClient:
    """ai_client 替身：不真发 HTTP，只记录调用参数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def trigger_distill(self, article_id: str, auth_token: str | None = None, **_kw):
        self.calls.append({"article_id": article_id, "auth_token": auth_token})
        return {
            "article_id": article_id,
            "task_id": f"dst_{uuid.uuid4().hex[:24]}",
            "status": "started",
        }


async def article_row(article_id: str) -> Article | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Article).where(Article.id == article_id))
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# 5.1 URL 解析（2）
# ---------------------------------------------------------------------------
def test_extract_url_basic():
    assert _extract_url("https://example.com/article") == "https://example.com/article"
    assert _extract_url("推荐 https://example.com/x 看") == "https://example.com/x"
    assert _extract_url("没有链接的一段话") is None


def test_extract_url_with_trailing_punctuation():
    # 末尾中文句号 / 逗号 / 分号 / 引号都要 trim
    for punct in ("。", "，", "；", "！", "”", ")", "."):
        assert _extract_url(f"推荐 https://example.com/x{punct}") == "https://example.com/x"
    # 路径中间的标点不动
    assert _extract_url("https://example.com/a,b") == "https://example.com/a,b"


# ---------------------------------------------------------------------------
# 5.2 URL 合法性（3）
# ---------------------------------------------------------------------------
def test_is_valid_url_http_https():
    assert _is_valid_url("https://example.com/a") is True
    assert _is_valid_url("http://example.com") is True


def test_is_valid_url_rejects_no_scheme():
    assert _is_valid_url("example.com") is False
    assert _is_valid_url("//example.com/a") is False


def test_is_valid_url_rejects_javascript_scheme():
    assert _is_valid_url("javascript:alert(1)") is False
    assert _is_valid_url("data:text/html,<script>") is False


# ---------------------------------------------------------------------------
# 5.3 map_fetcher_error（5）
# ---------------------------------------------------------------------------
def test_map_unsupported_to_2001():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.UNSUPPORTED, "placeholder", source="wechat_mp")
    )
    assert exc.code == 2001
    assert exc.http_status == 400
    assert "not supported" in exc.message


def test_map_network_to_2002():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.NETWORK, "timeout after 30s", source="generic_url")
    )
    assert exc.code == 2002
    assert exc.http_status == 502
    assert "generic_url/fetcher.network" in exc.message


def test_map_parse_to_2002():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.PARSE, "empty html body", source="generic_url")
    )
    assert exc.code == 2002
    assert exc.http_status == 502


def test_map_auth_to_2002():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.AUTH, "login required", source="wechat_mp")
    )
    assert exc.code == 2002
    assert exc.http_status == 502


def test_map_internal_to_2002():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.INTERNAL, "boom", source="douyin")
    )
    assert exc.code == 2002
    assert exc.http_status == 500  # INTERNAL 与其它 2002 不同：500 不是 502


# ---------------------------------------------------------------------------
# 5.4 端点 E2E（1）
# ---------------------------------------------------------------------------
_FAKE_HTML = (
    "<html><head>"
    '<meta property="og:title" content="听匣服务号测试标题">'
    "</head><body>"
    "<p>这是一段足够长的正文内容，用来通过正文抽取的密度与长度阈值检查，确保抓取成功。</p>"
    "</body></html>"
)


def _mock_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=_FAKE_HTML, headers={"content-type": "text/html; charset=utf-8"}
    )


@pytest.mark.asyncio
async def test_wechat_mp_message_happy_path(monkeypatch):
    """MockTransport 拦截 GenericURLFetcher → 建 article + 触发 distill。"""
    fake_ai = FakeAIClient()
    monkeypatch.setattr(content_main, "get_ai_client", lambda: fake_ai)
    # 只留 generic_url（带 mock transport）：公众号 fetcher 还是占位实现
    monkeypatch.setattr(
        cs_fetchers,
        "_ALL_FETCHERS",
        [cs_fetchers.GenericURLFetcher(transport=httpx.MockTransport(_mock_transport))],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_main.app), base_url="http://test"
    ) as c:
        r = await c.post(
            MP_URL,
            json={
                "from_user": "o_openid_123",
                "text": "推荐 https://example.com/article 看看。",
                "create_time": 1700000000,
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] is True
    assert body["article_id"].startswith("art_")
    assert body["task_id"].startswith("dst_")
    assert body["title"] == "听匣服务号测试标题"  # fetcher 抓到的 title 落到 articles
    assert body["source"] == "wechat_mp"

    art = await article_row(body["article_id"])
    assert art is not None
    assert art.url == "https://example.com/article"  # 末尾句号已 trim
    assert art.user_id == content_main.ANONYMOUS_USER_ID  # 匿名 user_id=0
    assert art.status == "pending"

    assert len(fake_ai.calls) == 1
    assert fake_ai.calls[0]["article_id"] == body["article_id"]
