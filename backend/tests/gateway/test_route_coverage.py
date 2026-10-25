"""网关路由表 ↔ 下游实现 ↔ 客户端调用 三者的一致性守卫。

## 为什么要有这条测试

这个仓库的 `api-gateway/config.py` 里记着**同款事故的三次**：

    · notifications/{id}/mark-read 漏注册 → 角标永远不减少
    · admin/export/tags.csv 未实现 → 必 404
    · admin/llm/* 漏注册 → 8100 返 "no downstream route"

2026-10-22 的走查用 AST 差集一次抓出了**第四例**：

    POST /api/v1/users/me/quota/reset-monthly

实现一直在 user-service 里，也进了 OpenAPI 契约，但**没进路由表** →
走 proxy_fallback 时首段 `users`（复数）不在白名单 → 网关直接
404「接口不存在」。实测确认：网关 404，直连 user-service 才是 401。

这四次是**同一个形态**：代码写了，路由（或步骤）没跟上。
人眼比对发现不了第 N+1 次 —— 所以要一条自动的守卫。

## 这条测试锁住两件事

A. **路由表里每一条都有下游实现**（不能注册一个下游没有的端点 →
   代理过去必然 404，而且报错信息指向网关，看不出是配置漏了）。

B. **下游每个端点网关都路由得到**（要么显式注册，要么前缀 fallback
   能猜到）。这一条才是真正防住上面四次事故的 —— A 只能防住反向。

B 的判定刻意**放宽**：fallback 能猜到也算过。理由是 fallback 现在还在
用（11 个端点走它），硬性要求全部显式注册会让这条测试变成"重构清单"，
而不是"事故防线"。但只要 fallback 也路由不到，就必须失败 —— 那就是
「端点存在但调用方永远拿不到」。

## 口径

- 端点用 **AST** 抽取，不用 grep：grep 分不清 `@app.get("/x")` 与
  `@app.get("/x", ...)`，也分不清字符串拼接的路径。
- 路径模板按 FastAPI 的写法比对（`{article_id}` 两边写法一致）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
GATEWAY_DIR = BACKEND_DIR / "api-gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from config import ROUTES  # noqa: E402

# 下游服务 → 声明路由的文件。
#
# admin_router.py 里的 router 被 main.py 用 include_router 装上，
# 所以它们的路径是**完整路径**（不带 prefix），和 ROUTES 里写的一样。
SERVICE_FILES = {
    "content-service": [
        BACKEND_DIR / "content-service" / "main.py",
        BACKEND_DIR / "content-service" / "admin_router.py",
        BACKEND_DIR / "content-service" / "tts_voice_router.py",
    ],
    "user-service": [BACKEND_DIR / "user-service" / "main.py"],
    "ai-service": [
        BACKEND_DIR / "ai-service" / "main.py",
        BACKEND_DIR / "ai-service" / "admin_router.py",
    ],
}

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _decorated_routes(path: Path) -> set[tuple[str, str]]:
    """抽一个文件里所有 @app.<method>("/path") / @router.<method>("/path")。"""
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr.lower() not in HTTP_METHODS:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id not in ("app", "router"):
                continue
            if not dec.args:
                continue
            first = dec.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            found.add((func.attr.upper(), first.value))
    return found


def _all_service_routes() -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for service, files in SERVICE_FILES.items():
        routes: set[tuple[str, str]] = set()
        for f in files:
            routes |= _decorated_routes(f)
        out[service] = routes
    return out


def _normalize(path: str) -> str:
    """把路径模板归一化，好让 {id} 与 {article_id} 这类差异不误报。"""
    return path.rstrip("/") or "/"


@pytest.fixture(scope="module")
def service_routes():
    return _all_service_routes()


@pytest.fixture(scope="module")
def fallback_targets():
    """网关兜底能解析到的下游服务（复用网关自己的判定，不抄一份）。"""
    import main as gw  # noqa: PLC0415

    return gw._resolve_target


def test_route_table_is_not_empty():
    assert len(ROUTES) > 50, "路由表空了？多半是导入方式变了"


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_registered_route_has_downstream_implementation(route, service_routes):
    """A. 路由表里每一条都要有下游实现。"""
    impl = service_routes.get(route.target_service)
    assert impl is not None, f"未知的目标服务: {route.target_service}"
    key = (route.method, _normalize(route.path))
    assert key in impl, (
        f"路由表注册了 {route.method} {route.path} → {route.target_service}，"
        f"但该服务里找不到这个端点。代理过去必然 404，"
        f"而且报错指向网关、看不出是配置漏了。"
    )


def test_every_downstream_endpoint_is_reachable_through_gateway(service_routes, fallback_targets):
    """B. 下游每个端点，网关都要路由得到（显式注册或前缀 fallback 猜得到）。

    这条才是真正防住那四次事故的断言。

    ⚠️ 唯一例外是 `/api/v1/internal/` 前缀（**服务间**端点）：
    那类端点的调用方是**另一个服务**（走 127.0.0.1 直连），
    不是客户端 ⇒ 暴露到公网网关只会多一个攻击面。
    这条不变量在内部端点上**反向**成立 —— 见
    `test_internal_endpoints_are_not_exposed_through_gateway`。
    """
    unreachable: list[str] = []
    for service, impl in service_routes.items():
        for method, path in sorted(impl):
            if not path.startswith("/api/v1/"):
                continue  # 网关不代理非 /api/v1 的路径
            if method == "HEAD":
                continue  # FastAPI 的 GET 会自动带 HEAD
            if path in {"/api/v1/health", "/api/v1/healthz"}:
                continue
            if path.startswith("/api/v1/internal/"):
                continue  # 服务间端点，见上面的说明与反向断言
            # 显式注册？
            if any(
                r.method == method and _normalize(r.path) == _normalize(path)
                for r in ROUTES
                if r.target_service == service
            ):
                continue
            # 兜底能猜到？
            if fallback_targets(path[len("/api/v1/") :]) is not None:
                continue
            unreachable.append(f"{method} {path} （{service}）")

    assert not unreachable, (
        "这些端点下游实现了，但网关**路由不到** —— 调用方永远拿不到：\n  "
        + "\n  ".join(unreachable)
        + "\n修法：加进 api-gateway/config.py 的 ROUTES，或让 proxy_fallback 的"
        "前缀白名单能认出它。"
    )


def test_fallback_whitelist_covers_users_prefix(fallback_targets):
    """兜底白名单必须认得 `users`（复数）。

    `users` 下的端点（consent / devices / account/delete / quota/reset-monthly）
    全是**复数**段名，网关的白名单原来只有 `user`（单数）→ 这些路径一旦
    漏注册就 404「接口不存在」，而实现就在 user-service 里。
    """
    assert fallback_targets("users/me/quota/reset-monthly") is not None


def test_internal_endpoints_are_not_exposed_through_gateway(service_routes, fallback_targets):
    """反向不变量：`/api/v1/internal/` 的端点**不许**被网关暴露。

    为什么要有这条：内部端点的调用方是**另一个服务**（走 127.0.0.1 直连），
    不是客户端。暴露到公网网关只会多一个攻击面 —— 哪怕它有共享密钥兜底，
    也是「本来不需要暴露的东西不要暴露」。

    两条路都堵死：
    ① 显式路由表里不许有 internal；
    ② 兜底白名单不许猜得到 `internal/...`
       （`proxy_fallback` 按第一段路径猜下游，`internal` 不在白名单里
       ⇒ 猜不到 ⇒ 网关自己答 404「接口不存在」）。
    """
    internal = {
        (method, path)
        for impl in service_routes.values()
        for method, path in impl
        if path.startswith("/api/v1/internal/")
    }
    assert internal, "没有内部端点？这条断言就失去意义了（端点被改名/删掉了）"

    for method, path in sorted(internal):
        assert not any(
            r.method == method and _normalize(r.path) == _normalize(path) for r in ROUTES
        ), f"{method} {path} 被显式注册进网关路由表了 —— 内部端点不该暴露到公网"

    assert (
        fallback_targets("internal/articles/x/captions") is None
    ), "兜底白名单能猜到 internal/... ⇒ 网关会把内部端点代理出去"
