"""网关**透传下游响应头**的回归（2026-10-25 修）。

## 为什么要有这个文件

修之前，`api-gateway` 的 `proxy()` 只把下游的 `content-type` 拿过来，
**其余响应头全丢**。丢掉的那几个恰好是**业务语义头**：

  · `Deprecation: true` / `Sunset: CP12` / `Link: rel="successor-version"`
    —— content-service 的 `POST /api/v1/articles/add` 靠这三个头告诉客户端
    「这个端点要下线了，请迁到新端点」。头丢了 ⇒ 这句话到不了客户端 ⇒
    老客户端永远不迁 ⇒ 弃用机制在生产**完全失效**。

这与「旧端点不扣配额」那个缺陷互为因果：不扣配额 ⇒ 用户没理由迁 ⇒
弃用提示又传不出去 ⇒ 缺口永远开着。

而且**两条代理路径都中招**：显式路由表（`proxy()`）与兜底路由
（`proxy_fallback()`）各写了一份响应构造 —— 两处都只透传 content-type。

## 覆盖到的行为

· 白名单内的头（弃用契约 / 缓存 / 重试 / 下载）**透传**到客户端；
· 白名单外的头（内部调试头、Set-Cookie、Location）**不透传**；
· 同一个头出现多次（`Link`）按 HTTP 语义合并成一个；
· 显式路由与兜底路由**口径一致**；
· 端到端：真实 content-service 的弃用头，经网关后真的到客户端。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.responses import Response

from helpers import new_user

ARTICLES_ADD_URL = "/api/v1/articles/add"


def _stub_with_headers(headers: dict[str, str], raw_extra: list[tuple[bytes, bytes]] | None = None):
    """造一个「带指定响应头」的上游替身。

    `raw_extra` 用来造**重复的头**（`Link` 可以有多条），dict 做不到。
    """

    def _make() -> FastAPI:
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def _stub():
            resp = Response(content=b'{"ok":true}', media_type="application/json", headers=headers)
            for k, v in raw_extra or []:
                resp.raw_headers.append((k, v))
            return resp

        return app

    return _make


# ---------------------------------------------------------------------------
# 1. 白名单内的头透传
# ---------------------------------------------------------------------------
async def test_下游业务响应头被透传(gw, mount):
    mount(
        _stub_with_headers(
            {
                "Deprecation": "true",
                "Sunset": "CP12",
                "Link": '</api/v1/articles>; rel="successor-version"',
                "Retry-After": "30",
                "Cache-Control": "no-store",
            }
        )()
    )

    r = await gw.get("/api/v1/articles/pending")

    assert r.status_code == 200
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "CP12"
    assert "successor-version" in (r.headers.get("Link") or "")
    assert r.headers.get("Retry-After") == "30"
    assert r.headers.get("Cache-Control") == "no-store"
    # content-type 不能被透传逻辑弄坏
    assert r.headers.get("content-type", "").startswith("application/json")


# ---------------------------------------------------------------------------
# 2. 白名单外的头不透传
# ---------------------------------------------------------------------------
async def test_白名单外的头不透传(gw, mount):
    """内部调试头 / Set-Cookie / Location 一律不转发。

    - 内部调试头：内部信息不该出网关；
    - Set-Cookie：cookie 的 Domain/Path 是按下游域写的，转到网关域上作用域错乱；
    - Location：下游将来若返回 302，里面很可能是内网地址 ⇒ 泄露内部拓扑。
    """
    mount(
        _stub_with_headers(
            {
                "X-Internal-Debug": "db-password-please-ignore",
                "Set-Cookie": "sid=abc; Domain=content-service; Path=/",
                "Location": "http://content-service:8102/internal",
                "Server": "nginx/1.0",
            }
        )()
    )

    r = await gw.get("/api/v1/articles/pending")

    assert r.status_code == 200
    assert "X-Internal-Debug" not in r.headers
    assert "Set-Cookie" not in r.headers
    assert "Location" not in r.headers


# ---------------------------------------------------------------------------
# 3. 重复的 Link 合并成一个
# ---------------------------------------------------------------------------
async def test_重复的Link按HTTP语义合并(gw, mount):
    mount(
        _stub_with_headers(
            {},
            raw_extra=[
                (b"link", b'</api/v1/articles>; rel="successor-version"'),
                (b"link", b'</api/v1/articles/document>; rel="alternate"'),
            ],
        )()
    )

    r = await gw.get("/api/v1/articles/pending")

    link = r.headers.get("Link") or ""
    assert "successor-version" in link
    assert "alternate" in link
    assert "," in link, "两条 Link 应合并成一个头（RFC 9110 §5.3）"


# ---------------------------------------------------------------------------
# 4. 兜底路由与显式路由口径一致
# ---------------------------------------------------------------------------
async def test_兜底路由同样透传响应头(gw, mount):
    """`proxy_fallback` 曾经**另写一份**响应构造（同样只透传 content-type）。

    两处各写一份就会各自漂 —— 这里锁住「两条路径口径一致」。
    """
    mount(_stub_with_headers({"Deprecation": "true", "Sunset": "CP12"})())

    # `/api/v1/articles/{id}/status` 之类的路径不在显式路由表里 → 走兜底
    r = await gw.get("/api/v1/articles/art_fallback_probe/status")

    assert r.status_code == 200
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "CP12"


# ---------------------------------------------------------------------------
# 5. 端到端：真实 content-service 的弃用头经网关后真的到客户端
# ---------------------------------------------------------------------------
async def test_旧端点的弃用头经网关后到客户端(gw):
    """这一条是整个缺陷的**真实现场**。

    修之前：客户端拿到的响应里**没有** Deprecation 头 ⇒ 老客户端不知道
    要迁移 ⇒ 弃用机制形同虚设。
    """
    _uid, token = await new_user()

    r = await gw.post(
        ARTICLES_ADD_URL,
        json={"url": f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex[:8]}", "source": "wechat"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert r.headers.get("Deprecation") == "true", "弃用契约头没到客户端"
    assert r.headers.get("Sunset") == "CP12"
    assert "successor-version" in (r.headers.get("Link") or "")
