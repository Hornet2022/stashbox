"""CP2.5 服务号 Handler 单测（14 个 case）。

覆盖：URL 解析 2 + URL 合法性 3 + FetcherError→BizException 映射 5 + 端点 E2E 1
+ raw_content 落库 3（CP-CREATE-ARTICLE：1 单元 + 2 E2E）。

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
from datetime import datetime
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

from stashbox.backend.common.database import AsyncSessionLocal, engine  # noqa: E402
from stashbox.backend.common.models import Article  # noqa: E402

MP_URL = "/api/v1/callback/wechat-mp-message"

# 端点挂了 `require_callback_secret` 之后（fail-closed：没配密钥就 503），
# 这批 E2E 一直没跟着改 —— 因为它们不在 CI 里，跑红也没人知道。
_TEST_SECRET = "test-callback-secret"


@pytest.fixture
def callback_secret(monkeypatch):
    """配好回调密钥，返回要随请求带上的请求头。

    用 monkeypatch 改 settings 实例而不是改环境变量：pydantic v2 的 Settings
    在**构造时**就把环境变量读进字段了，事后再 os.environ[...] = ... 不会生效
    （这个坑踩过一次：设了变量却仍返回 503）。
    """
    from stashbox.backend.common.config import settings

    monkeypatch.setattr(settings, "callback_shared_secret", _TEST_SECRET, raising=False)
    return {"X-Callback-Secret": _TEST_SECRET}


@pytest.fixture(autouse=True)
async def _dispose_pools():
    """每个 case 结束释放 DB/Redis 连接池。

    pytest-asyncio 为每个 async test 建新 event loop，而连接池是模块级全局的，
    不释放会把上一个 loop 的连接带到下一个 case（asyncpg: attached to a different
    loop；redis-py: Event loop is closed）。做法同 backend/tests/conftest.py。
    """
    yield
    from stashbox.backend.common import redis_client

    await engine.dispose()
    pool = redis_client._redis_pool
    if pool is not None:
        await pool.disconnect(inuse_connections=True)
        redis_client._redis_pool = None


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
    # 2026-10-03：message 改成面向用户的中文，技术细节挪到 detail。
    # 原来 message 里塞 "url not supported: ..." 这种英文 + 内部代号，
    # 终端用户看不懂也不知道该换什么链接 —— 剪藏失败率的一大来源。
    assert "暂不支持" in exc.message
    assert "not supported" not in exc.message
    assert "placeholder" in exc.detail


def test_map_network_to_2002():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.NETWORK, "timeout after 30s", source="generic_url")
    )
    assert exc.code == 2002
    assert exc.http_status == 502
    assert "没能连上" in exc.message
    # 技术细节（哪个 fetcher / 哪类错误）留给日志，不进响应体
    assert "generic_url/fetcher.network" in exc.detail
    assert "generic_url" not in exc.message


def test_map_auth_message_tells_user_what_to_do():
    """微信风控是最高频的抓取失败，文案必须给出可操作的下一步。"""
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.AUTH, "wechat requires in-app browser", source="wechat_mp")
    )
    assert exc.code == 2002
    assert "微信" in exc.message and "复制链接" in exc.message
    # 内部代号 / 英文异常信息一律不出现在 message
    assert "wechat_mp" not in exc.message
    assert "in-app browser" not in exc.message


def test_map_not_found_message():
    exc = map_fetcher_error(
        FetcherError(FetcherErrorCode.NOT_FOUND, "http 404", source="wechat_mp")
    )
    assert exc.code == 2002
    assert "删除" in exc.message or "失效" in exc.message


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
    exc = map_fetcher_error(FetcherError(FetcherErrorCode.INTERNAL, "boom", source="douyin"))
    assert exc.code == 2002
    assert exc.http_status == 500  # INTERNAL 与其它 2002 不同：500 不是 502


# ---------------------------------------------------------------------------
# 5.4 端点 E2E（1）
# ---------------------------------------------------------------------------
# 正文长度按生产阈值 MIN_ARTICLE_CHARS 生成，不写死数字。
#
# 这段夹具原来只有 39 个字，注释还写着「足够长……通过长度阈值检查」——
# 作者的**意图**是对的，只是后来 parser 把门槛提到 200 字（MIN_ARTICLE_CHARS，
# 理由是「低于 200 字的网页听完不到 1 分钟」），夹具没跟着改。因为这批用例不在
# CI 里，跑红也没人知道，于是它一直烂在这儿。
#
# 更要紧的是：这个失败长得像「测试数据不严谨」，实际是**夹具和生产规则脱钩**。
# 所以正文由阈值生成，并配一条用例守住（见 test_夹具正文_真的过得了生产阈值）——
# 将来再调阈值，这里会自动跟着变，不会又悄悄烂掉。
_BODY_SENTENCE = "这是一段足够长的正文内容，用来通过正文抽取的密度与长度阈值检查，确保抓取成功。"


def _body_paragraph() -> str:
    """生成一段正文，长度明确超过生产要求的 MIN_ARTICLE_CHARS。"""
    need = cs_fetchers.parser.MIN_ARTICLE_CHARS + 50
    reps = -(-need // len(_BODY_SENTENCE))  # 向上取整
    return "<p>" + _BODY_SENTENCE * reps + "</p>"


_FAKE_HTML = (
    "<html><head>"
    '<meta property="og:title" content="听匣服务号测试标题">'
    "</head><body>" + _body_paragraph() + "</body></html>"
)


def _mock_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=_FAKE_HTML, headers={"content-type": "text/html; charset=utf-8"}
    )


@pytest.mark.asyncio
async def test_wechat_mp_message_happy_path(monkeypatch, callback_secret):
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
            headers=callback_secret,
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


# ---------------------------------------------------------------------------
# 5.5 raw_content 落库（CP-CREATE-ARTICLE，3）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_article_with_raw_content():
    """_create_article 传 raw_content → 落库到 articles.raw_content JSONB。"""
    async with AsyncSessionLocal() as db:
        art = await content_main._create_article(
            "https://example.com/raw-content-unit",
            content_main.ANONYMOUS_USER_ID,
            "unit_test",
            "Unit Title",
            db,
            raw_content={"title": "x", "content_text": "y", "media_urls": ["http://img.jpg"]},
        )
        try:
            result = await db.execute(select(Article).where(Article.id == art.id))
            row = result.scalar_one()
            assert row.raw_content["content_text"] == "y"
            assert row.raw_content["media_urls"] == ["http://img.jpg"]
        finally:
            await db.delete(art)
            await db.commit()


_HTML_WITH_MEDIA = (
    "<html><head>"
    '<meta property="og:title" content="带图片的原文标题">'
    '<meta property="article:published_time" content="2026-01-15T08:30:00+00:00">'
    "</head><body>"
    + _body_paragraph()
    + '<p><img src="https://example.com/pic1.jpg"></p>'
    + "</body></html>"
)


def _mock_transport_with_media(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=_HTML_WITH_MEDIA, headers={"content-type": "text/html; charset=utf-8"}
    )


@pytest.mark.asyncio
async def test_handler_stores_fetch_result_raw_content(monkeypatch, callback_secret):
    """Handler 把 FetchResult 全字段落到 articles.raw_content。"""
    monkeypatch.setattr(content_main, "get_ai_client", lambda: FakeAIClient())
    monkeypatch.setattr(
        cs_fetchers,
        "_ALL_FETCHERS",
        [cs_fetchers.GenericURLFetcher(transport=httpx.MockTransport(_mock_transport_with_media))],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_main.app), base_url="http://test"
    ) as c:
        r = await c.post(
            MP_URL,
            headers=callback_secret,
            json={
                "from_user": "o_openid_123",
                "text": "https://example.com/media",
                "create_time": 1,
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    art = await article_row(body["article_id"])
    try:
        assert art is not None
        assert art.raw_content["source"] == "generic_url"
        assert "足够长的正文内容" in art.raw_content["content_text"]
        assert art.raw_content["media_urls"] == ["https://example.com/pic1.jpg"]
        # datetime → ISO 字符串（否则 JSONB 序列化会失败）
        assert art.raw_content["publish_time"] == "2026-01-15T08:30:00+00:00"
    finally:
        async with AsyncSessionLocal() as session:
            await session.delete(art)
            await session.commit()


@pytest.mark.asyncio
async def test_handler_response_includes_fetch_metadata(monkeypatch, callback_secret):
    """Handler 响应含 fetched_at / content_text_length / has_media。"""
    monkeypatch.setattr(content_main, "get_ai_client", lambda: FakeAIClient())
    monkeypatch.setattr(
        cs_fetchers,
        "_ALL_FETCHERS",
        [cs_fetchers.GenericURLFetcher(transport=httpx.MockTransport(_mock_transport_with_media))],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_main.app), base_url="http://test"
    ) as c:
        r = await c.post(
            MP_URL,
            headers=callback_secret,
            json={
                "from_user": "o_openid_123",
                "text": "https://example.com/meta",
                "create_time": 1,
            },
        )

    assert r.status_code == 200, r.text
    data = r.json()
    datetime.fromisoformat(data["fetched_at"])  # ISO 字符串可解析
    assert data["content_text_length"] > 0
    assert data["has_media"] is True


# ---------------------------------------------------------------------------
# 夹具自身的守卫（防止这段腐烂重演）
# ---------------------------------------------------------------------------


def test_夹具正文_真的过得了生产阈值():
    """把夹具丢给**生产解析器**，确认它真能抽出 ≥ MIN_ARTICLE_CHARS 的正文。

    不这么测的话，「夹具太短」这件事只有在跑端点 E2E 时才会以 502 的形式
    露出来 —— 而报错信息是「没能提取出文章正文」，指向的是解析器，
    没人会想到是夹具烂了。这就是它能一直烂在 CI 之外的原因。
    """
    parser = cs_fetchers.parser

    text = parser.ContentExtractor().parse(_FAKE_HTML).content_text()

    assert text, "夹具连正文都没解析出来"
    assert len(text) >= parser.MIN_ARTICLE_CHARS, (
        f"夹具正文只有 {len(text)} 字，低于生产阈值 {parser.MIN_ARTICLE_CHARS} —— "
        "端点 E2E 会以 502「没能提取出文章正文」失败，而报错会指向解析器"
    )


def test_带图片夹具_同样过得了生产阈值():
    """第二个夹具同样守住：只测第一个的话，改第二个时会重演。"""
    parser = cs_fetchers.parser

    text = parser.ContentExtractor().parse(_HTML_WITH_MEDIA).content_text()

    assert text
    assert len(text) >= parser.MIN_ARTICLE_CHARS, f"只有 {len(text)} 字"


def test_夹具正文长度_超过阈值():
    """防止有人把 _body_paragraph 改回固定短字符串。"""
    assert len(_body_paragraph()) > cs_fetchers.parser.MIN_ARTICLE_CHARS
