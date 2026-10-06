"""
CP1.7 D9 入口单测：POST /api/v1/callback/d9-add-article。

覆盖：匿名 device_id / 已登录扣配额 / 配额用尽 3001 / URL 不合法 2001 /
      无身份 4001 / ai-service 失败仍建文章 / **抓取失败当场报错（2026-10 新增）**

2026-10 修用例（两处，都是真 bug 而不只是测试过期）：

1. **本文件原先会打真网。** `fake_ai_client` 只挡了 ai-service，抓取链路没人管，
   于是每个用例都真的 GET `https://mp.weixin.qq.com/s/...`，还会升级到无头浏览器
   层——单文件能跑到 >45s 把整套测试拖死，且离线/CI 环境必挂。现在由
   `fake_fetcher` fixture 把 `get_fetcher` 整体换掉，一个外网请求都不发。

2. **用例 1/2/3/6 断言的是 10-03 推翻的旧契约。** 那次「剪藏抓取失败当场报错，
   不再软降级进 LLM 蒸馏」把抓取提到了建文章之前，失败即 502。旧用例仍然
   `assert 200`，实际早已红。另有一条更关键的缺口：抓取失败这条新路径
   **一个用例都没有**——它正是剪藏成功率的生命线，必须锁住「不建文章、不扣配额」。
"""

import httpx
import uuid

import pytest

from stashbox.backend.common.auth import decode_token

# content-service/main.py:50 把自己所在目录插进了 sys.path，所以这里能直接
# 按包名导入 fetchers（与 main.py 内部 `from fetchers import ...` 同一套模块）。
from fetchers import FetcherError, FetcherErrorCode
from fetchers.base import FetchResult

from helpers import (
    ai_client,
    article_row,
    article_row_by_url,
    client,
    content_main,
    new_user,
    quota_used,
)

ANONYMOUS_USER_ID = content_main.ANONYMOUS_USER_ID
D9_URL = "/api/v1/callback/d9-add-article"


# ---------------------------------------------------------------------------
# 抓取层 mock：切断一切外网请求
# ---------------------------------------------------------------------------
class _FakeFetcher:
    """最小 fetcher 替身：只实现 D9 链路用到的那一个方法。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[str] = []

    async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return FetchResult(
            url=url,
            title="测试文章标题",
            content_html="<p>正文</p>",
            content_text="测试正文内容",
            source="wechat_mp",
        )


@pytest.fixture(autouse=True)
def fake_fetcher(monkeypatch) -> _FakeFetcher:
    """默认把 `get_fetcher` 换成成功路径的假抓取器。

    autouse：这条链路上的每个用例都必须走 mock，否则会真去连 mp.weixin.qq.com。
    需要模拟失败的用例用 `set_fetcher_error` 覆盖。
    """
    fetcher = _FakeFetcher()
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    return fetcher


def set_fetcher_error(monkeypatch, code: FetcherErrorCode, message: str = "boom") -> _FakeFetcher:
    """把 fake_fetcher 切成「抓取失败」路径。"""
    fetcher = _FakeFetcher(FetcherError(code, message, source="wechat_mp"))
    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: fetcher)
    return fetcher


# ---------------------------------------------------------------------------
# 1. 匿名（device_id only）→ 建文章 + 不扣配额 + 回显 device_id
# ---------------------------------------------------------------------------
async def test_d9_anonymous_device_id_creates_article_without_quota(fake_ai_client):
    device_id = "device_" + uuid.uuid4().hex[:8]
    # 端点 Header alias 为 X-Device-Id（大小写不敏感），helper 会把下划线转连字符
    async with client(x_device_id=device_id) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_anon"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["device_id"] == device_id
    assert body["status"] == "distilling"  # ai-service 已接单
    assert body["task_id"].startswith("dst_")

    art = await article_row(body["article_id"])
    assert art is not None
    assert art.user_id == ANONYMOUS_USER_ID  # 匿名用 user_id=0 标记
    assert art.source == "wechat"
    assert await quota_used(ANONYMOUS_USER_ID) == 0  # 匿名不计费


# ---------------------------------------------------------------------------
# 2. 已登录（JWT）→ 扣 1 次配额 + 带 owner JWT 触发 ai-service
# ---------------------------------------------------------------------------
async def test_d9_logged_in_consumes_quota_once(fake_ai_client):
    uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_user"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_id"].startswith("art_")
    assert body["device_id"] is None

    assert await quota_used(uid) == 1  # D9 预扣 1 次

    # 触发 ai-service：带上 owner 的 JWT（ai-service 的 distill 端点有 require_user）
    assert len(fake_ai_client.calls) == 1
    call = fake_ai_client.calls[0]
    assert call["article_id"] == body["article_id"]
    assert int(decode_token(call["auth_token"])["sub"]) == uid


# ---------------------------------------------------------------------------
# 3. 配额用尽 → 3001
# ---------------------------------------------------------------------------
async def test_d9_quota_exhausted_returns_3001():
    _uid, token = await new_user(monthly_quota=1)

    async with client(token) as c:
        first = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_ok"})
        assert first.status_code == 200, first.text

        second = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_over"})

    assert second.status_code == 403, second.text
    assert second.json()["code"] == 3001  # v1 §3.0：月配额用尽


# ---------------------------------------------------------------------------
# 4. URL 不合法（非 http/https）→ 2001
# ---------------------------------------------------------------------------
async def test_d9_invalid_url_returns_2001(fake_ai_client):
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "ftp://example.com/a.mp3"})

    assert r.status_code == 400, r.text
    assert r.json()["code"] == 2001  # v1 §3.0：URL scheme 不支持
    assert fake_ai_client.calls == []  # 没建文章，也没触发蒸馏


# ---------------------------------------------------------------------------
# 5. 既无 JWT 也无 device_id → 4001
# ---------------------------------------------------------------------------
async def test_d9_missing_identity_returns_4001(fake_ai_client):
    async with client() as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_none"})

    assert r.status_code == 400, r.text
    assert r.json()["code"] == 4001
    assert fake_ai_client.calls == []


# ---------------------------------------------------------------------------
# 6. ai-service 调用失败 → D9 仍成功建文章，task_id=None
# ---------------------------------------------------------------------------
async def test_d9_ai_service_failure_still_creates_article(fake_ai_client):
    fake_ai_client.fail = True
    _uid, token = await new_user()

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": "https://mp.weixin.qq.com/s/d9_ai_down"})

    assert r.status_code == 200, r.text  # 不抛 5xx —— D9 用户体验优先
    body = r.json()
    assert body["task_id"] is None
    assert body["status"] == "pending"

    art = await article_row(body["article_id"])
    assert art is not None and art.status == "pending"


# ---------------------------------------------------------------------------
# 7. 抓取失败 → 当场报错，不建文章、不扣配额（2026-10-03 行为，此前无用例）
#
# 这条是剪藏成功率的生命线。原来抓取失败被 `_create_article` 整个吞掉，文章照样
# 建成 pending + raw_content=None，蒸馏任务照派 —— ai-service 拿不到正文就把
# `"[empty article] ..."` 当正文喂给 LLM。后果链：扣 1 次配额 → 白烧 token →
# 用户等 12 分钟看到失败，而剪藏接口当时返回 200。
#
# 改为「先抓后建，抓不到就当场带原因报错」后，必须锁住三件事：不建文章、
# 不扣配额、不派蒸馏任务。
# ---------------------------------------------------------------------------
async def test_d9_fetch_failure_errors_out_without_creating_article(monkeypatch, fake_ai_client):
    set_fetcher_error(monkeypatch, FetcherErrorCode.PARSE, "wechat article body not found")

    uid, token = await new_user(monthly_quota=5)
    url = "https://mp.weixin.qq.com/s/d9_fetch_fail"

    async with client(token) as c:
        r = await c.post(D9_URL, json={"url": url})

    # 2001 = 抓取失败（HTTP 502），且要带上用户看得懂的原因
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["code"] == 2002
    assert "正文" in body["message"]

    # 关键：不能建文章、不能扣配额、不能派蒸馏任务
    assert fake_ai_client.calls == []
    assert await quota_used(uid) == 0
    assert await article_row_by_url(url) is None


# ---------------------------------------------------------------------------
# 8. 抓取失败同样优先于「已登录」判定之外的身份分支：匿名剪藏也不建文章
# ---------------------------------------------------------------------------
async def test_d9_fetch_failure_anonymous_creates_nothing(monkeypatch, fake_ai_client):
    set_fetcher_error(monkeypatch, FetcherErrorCode.NETWORK, "connect timeout")
    device_id = "device_" + uuid.uuid4().hex[:8]
    url = "https://mp.weixin.qq.com/s/d9_anon_fail"

    async with client(x_device_id=device_id) as c:
        r = await c.post(D9_URL, json={"url": url})

    assert r.status_code == 502, r.text
    assert r.json()["code"] == 2002
    assert fake_ai_client.calls == []
    assert await article_row_by_url(url) is None


# ---------------------------------------------------------------------------
# 9-11. ai_client 本身：成功 / ai-service 5xx / 网络错误（含 retry）—— 失败只 log 不抛
# ---------------------------------------------------------------------------
def _patch_http(monkeypatch, handler) -> list:
    """把 AIServiceClient 内部的 httpx.AsyncClient 换成 MockTransport，记录请求。"""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    class _MockClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ai_client.httpx, "AsyncClient", _MockClient)
    return seen


async def test_ai_client_trigger_distill_success(monkeypatch):
    seen = _patch_http(
        monkeypatch,
        lambda _req: httpx.Response(
            200, json={"article_id": "art_1", "task_id": "dst_1", "status": "started"}
        ),
    )

    result = await ai_client.AIServiceClient().trigger_distill("art_1", auth_token="jwt_xxx")

    assert result["task_id"] == "dst_1"
    assert seen[0].url.path == "/api/v1/articles/art_1/distill"
    assert seen[0].headers["Authorization"] == "Bearer jwt_xxx"


async def test_ai_client_returns_none_on_http_500(monkeypatch):
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(500, json={"detail": "boom"}))

    assert await ai_client.AIServiceClient().trigger_distill("art_1") is None
    assert len(seen) == 1  # 确定性失败，不重试


async def test_ai_client_returns_none_on_transport_error(monkeypatch):
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ai-service down")

    seen = _patch_http(monkeypatch, _boom)

    assert await ai_client.AIServiceClient().trigger_distill("art_1") is None
    assert len(seen) == 2  # max_retries=2
