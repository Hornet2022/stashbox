"""网关在下游超时 / 不可达时必须返回**业务信封**，不能是裸 500。

2026-10-21 实跑撞出来的缺陷
---------------------------
剪藏（POST /api/v1/articles）在下游是**同步**的：建文章前必须先抓到正文。
抓取预算 STASHBOX_CAPTURE_FETCH_BUDGET 默认 30s，加上无头浏览器兜底的实际开销，
整条链最坏 ~40.6s（实测）。

而网关的 httpx timeout 当时是 30.0 —— **网关必然先超时**：

    直连 content-service:8102 → 502，40.6s，body = {"code":2002,"message":"没能连上这个网站…"}
    走网关 :8100            → 500，30.0s，body = Internal Server Error（纯文本）

httpx 的异常不是 HTTPException，绕过了 register_exception_handlers，
于是网关吐出的是 Starlette 默认的纯文本 500。后果不是"报错不好看"：

  - 安卓 parseEnvelope 解析不出 {code,message,data} → bizCode 落成 0
  - isCaptureFetchFailed(2001|2002) 恒为假 → 用户只看到"服务暂不可用"
  - 而下游那边文章其实**已经建好、配额已经扣了** → 用户以为失败，��反复重试

本文件锁住修后的行为：超时 / 连不上都必须给出可解析的信封，
且剪藏路径的文案要说清"可能已经提交"。
"""

import httpx
import pytest


@pytest.fixture(autouse=True)
async def upstream(gateway_app, content_base, upstream_requests):
    """覆盖 conftest 的默认上游（真实 content-service）→ 一个会失败的替身。

    失败方式由测试用例通过 `state["exc"]` 指定（超时 / 连接失败）。
    """
    state: dict = {"exc": httpx.ReadTimeout("simulated upstream timeout")}

    async def _record(request: httpx.Request) -> None:
        upstream_requests.append(request)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise state["exc"]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url=content_base,
        event_hooks={"request": [_record]},
    )
    gateway_app.state.httpx = client
    yield state
    await client.aclose()


# ---------------------------------------------------------------------------
# 1. 剪藏路径（未注册路由 → 走 proxy_fallback）超时 → 502 + 2002
# ---------------------------------------------------------------------------
async def test_capture_timeout_returns_biz_envelope(gw, upstream):
    """剪藏超时必须是 {code,message,data}，且业务码 2002（安卓认这个码）。"""
    upstream["exc"] = httpx.ReadTimeout("simulated upstream timeout")

    r = await gw.post("/api/v1/articles", json={"url": "https://example.com/a"})

    assert r.status_code == 502, r.text
    assert "application/json" in r.headers.get("content-type", "")
    body = r.json()
    assert body["code"] == 2002, body
    assert body["data"] is None
    # 文案必须说清"可能已经提交" —— 下游其实已经把文章建好、扣了配额
    assert "可能已经提交" in body["message"], body["message"]


# ---------------------------------------------------------------------------
# 2. 剪藏路径（已注册路由 → 走 proxy）超时 → 同样 502 + 2002
# ---------------------------------------------------------------------------
async def test_capture_add_registered_route_timeout_returns_biz_envelope(gw, upstream):
    """`/api/v1/articles/add` 在路由表里，走的是 proxy() 而非兜底 —— 两条路都得修。"""
    upstream["exc"] = httpx.ReadTimeout("simulated upstream timeout")

    r = await gw.post("/api/v1/articles/add", json={"url": "https://example.com/a"})

    assert r.status_code == 502, r.text
    body = r.json()
    assert body["code"] == 2002, body
    assert body["data"] is None


# ---------------------------------------------------------------------------
# 3. 非剪藏路径超时 → 504 + 50400
# ---------------------------------------------------------------------------
async def test_generic_timeout_returns_504_envelope(gw, upstream):
    """普通路径超时不冒充剪藏失败，给独立的 504。"""
    upstream["exc"] = httpx.ReadTimeout("simulated upstream timeout")

    r = await gw.get("/api/v1/articles/pending")

    assert r.status_code == 504, r.text
    body = r.json()
    assert body["code"] == 50400, body
    assert body["data"] is None
    assert body["message"]


# ---------------------------------------------------------------------------
# 4. 下游连不上（进程没起）→ 502 + 50200
# ---------------------------------------------------------------------------
async def test_connect_error_returns_biz_envelope(gw, upstream):
    """下游没起来是 50200，不是裸 500。"""
    upstream["exc"] = httpx.ConnectError("simulated connection refused")

    r = await gw.get("/api/v1/articles/pending")

    assert r.status_code == 502, r.text
    body = r.json()
    assert body["code"] == 50200, body
    assert body["data"] is None


# ---------------------------------------------------------------------------
# 5. 任何下游失败，响应体都必须是可解析的 JSON（守卫：不能退回纯文本）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("t"),
        httpx.ConnectError("c"),
        httpx.RemoteProtocolError("p"),
    ],
)
async def test_downstream_failure_body_is_always_json(gw, upstream, exc):
    """回归守卫：裸文本 "Internal Server Error" 就是修前的样子，必须不能复现。"""
    upstream["exc"] = exc

    r = await gw.get("/api/v1/articles/pending")

    assert r.status_code >= 400
    # 修前是 text/plain 的 "Internal Server Error"，json() 会直接抛
    body = r.json()
    assert set(body.keys()) >= {"code", "message", "data"}, body
    assert body["code"] != 0


# ---------------------------------------------------------------------------
# 6. 网关必须把真实客户端 IP 透给下游（限流/审计要用）
# ---------------------------------------------------------------------------
async def test_gateway_overwrites_x_forwarded_for(gw, upstream, upstream_requests):
    """网关转发给下游的 XFF，必须是**它自己解析出来的**真实客户端 IP。

    2026-10-21 改了语义：原来是"客户端带了 XFF 就透传"，等于把
    "客户端能伪造 XFF"这件事直接交给下游 —— 任何人自己发一个
    `X-Forwarded-For: 1.2.3.4` 就能绕过登录限流（每次换一个假 IP）。
    现在网关自己解析并**覆盖**。
    """
    upstream["exc"] = httpx.ReadTimeout("t")

    # 直连（测试客户端的 peer 不在可信代理列表）→ 伪造的 XFF 必须被忽略
    await gw.get("/api/v1/articles/pending", headers={"X-Forwarded-For": "203.0.113.7"})

    assert upstream_requests, "上游没收到请求"
    forwarded = upstream_requests[0].headers["x-forwarded-for"]
    assert forwarded != "203.0.113.7", "客户端伪造的 XFF 不能原样透传给下游"
    # 直连时对端地址就是测试客户端自己（Starlette TestClient 的 peer 是 127.0.0.1）
    assert forwarded == "127.0.0.1", f"应该换成网关看到的对端地址，实际 {forwarded!r}"
