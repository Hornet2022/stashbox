"""
api-gateway（端口 8100） - 听匣统一入口 + JWT 签发 + 路由分发。

职责：
  - GET  /health                                 健康检查
  - POST /api/v1/auth/token                     签发 JWT（公开）
  - 路由表命中的请求（见 config.ROUTES）          按表转发到对应下游
  - /api/v1/{path:path}                          表外兜底：按前缀猜下游

路由表（CP1.7.1）：route → service 的映射全部在 config.py，main.py 只做
动态注册 + 统一转发。CP1.7.1 新增 3 条：
  POST /api/v1/callback/d9-add-article      -> content-service（不要求登录态）
  GET  /api/v1/articles/{article_id}/status -> content-service
  GET  /api/v1/articles/{article_id}/audio-url -> content-service
  未匹配                                          -> 404
"""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

# 注意：本服务不再使用 sys.path hack。stashbox 包通过 PYTHONPATH（见 run_dev.sh）
# 或 `pip install -e` 导入。直接 `uvicorn main:app` 时需保证仓库根父目录在 PYTHONPATH 中。
# 例外：同目录下的 config.py（路由表）是「按文件加载」的一部分 —— 单测用
# importlib 加载 main.py 时 cwd 不在服务目录，故把自身目录加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402

import os

import httpx
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from config import D9_ROUTE, ROUTES, Route  # noqa: E402
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import register_exception_handlers
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.middleware.audit import AuditMiddleware
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.event_collect import router as event_router
from stashbox.backend.common.analytics import track_simple
from stashbox.backend.common.events import EventName
from stashbox.backend.common.database import AsyncSessionLocal


class ErrorTrackingMiddleware(BaseHTTPMiddleware):
    """5xx structlog 警告埋点（CP6.2.2.2b）。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.status_code >= 500:
            structlog.get_logger("api_5xx").warning(
                "api_5xx",
                path=request.url.path,
                status=response.status_code,
                method=request.method,
            )
        return response


setup_logging("api-gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.httpx = httpx.AsyncClient()
    # CP6.2.2.2b 埋点：SERVICE_START
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_START, 0, "n/a")
    except Exception:
        pass  # 失败不阻塞 startup
    yield
    # CP6.2.2.2b 埋点：SERVICE_STOP
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_STOP, 0, "n/a")
    except Exception:
        pass
    await app.state.httpx.aclose()


app = FastAPI(title="stashbox-api-gateway", version="0.1.0", lifespan=lifespan)
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(ErrorTrackingMiddleware)
app.add_middleware(AuditMiddleware)
install_health_endpoints(app)
# CORS: 本机开发默认放行所有 origin（admin-web dev 在 5173）。生产用环境
# 变量 STASHBOX_CORS_ALLOW_ORIGINS（逗号分隔）覆盖白名单。CP9.1.1 基础设施
# 修复 — admin-web 浏览器调 /api/v1/admin/* 跨源被浏览器拦截。
# 必须最后 add：FastAPI 中间件倒序执行（最后 add 最先 run = 最外层）。
import os as _os  # noqa: E402

_cors_env = _os.getenv("STASHBOX_CORS_ALLOW_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("/tmp/audio", exist_ok=True)
app.mount("/audio", StaticFiles(directory="/tmp/audio"), name="audio")


class TokenRequest(BaseModel):
    user_id: str


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int


@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-gateway"}


@app.post("/api/v1/auth/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest):
    """签发 JWT（mock：直接用传入 user_id）。"""
    token = create_access_token(req.user_id)
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


async def proxy(request: Request, route: Route) -> Response:
    """统一代理：method / path / query / body / headers 全透传给下游。

    CP1.7.1 不注入 service token（service-to-service auth 留 CP1.8+），
    Authorization 原样带给下游，由下游自己校验 JWT。
    """
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    # 链路串联：客户端没带 X-Request-ID 时补上 gateway 生成的那个，保证下游日志同源
    # （ASGI 把请求头名转成小写，这里按小写判存在，避免同一个头发两遍）
    if "x-request-id" not in {k.lower() for k in headers}:
        headers["X-Request-ID"] = request.state.request_id

    client: httpx.AsyncClient = request.app.state.httpx
    upstream = await client.request(
        request.method,
        f"{route.target_url}{request.url.path}",
        params=request.query_params,
        content=await request.body(),
        headers=headers,
        timeout=30.0,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def proxy_d9(request: Request) -> Response:
    """D9 回调：gateway 侧不强制 JWT —— Authorization 有就透传，没有就不带。

    承宇 2026-09-16 决策：身份判定交给下游 content-service 的
    require_user_optional（已登录用 user_id，未登录用 device-id 头，都没有 → 4001）。
    """
    return await proxy(request, D9_ROUTE)


# 按路由表动态注册（必须在下面的 /api/v1/{path:path} 兜底路由之前 —— Starlette
# 按注册顺序匹配，兜底路由放在后面才不会把精确路由吃掉）。
#
# CP1.7.3：这里早先用 functools.partial(proxy, route=_route) 绑定 route —— fastapi
# 0.141.1 不再解 partial 的签名，会把 Route 的字段（method / path / target_service）
# 当成 request body 模型去校验，POST 带 JSON body 一律 422（"missing field 'method'"）。
# 改用闭包工厂：注册的是普通 async 函数，签名里只剩 request，无 partial 解析问题。
def make_proxy(route: Route):
    """闭包工厂：捕获 route 变量，避开 functools.partial 的 fastapi 解析问题。"""

    async def _proxy(request: Request) -> Response:
        return await proxy(request, route)

    return _proxy


for _route in ROUTES:
    if _route.special == "d9":
        continue  # D9 单独挂（见下）：不要求登录态
    app.add_api_route(
        _route.path,
        make_proxy(_route),  # ← 普通函数，无 partial 解析问题
        methods=[_route.method],
        name=f"proxy_{_route.method.lower()}_{_route.path}",
    )

app.add_api_route(D9_ROUTE.path, proxy_d9, methods=["POST"], name="proxy_d9")

app.include_router(event_router)


def _resolve_target(path: str) -> str | None:
    """根据第一段路径决定下游 base url。"""
    # CP4.7.1: articles/{id}/distill 优先走 ai-service（fallback 误判会走 content-service → 404）
    if path.startswith("articles/") and "/distill" in path:
        return settings.ai_service_url
    segment = path.split("/", 1)[0]
    if segment in ("user", "subscription"):
        return settings.user_service_url
    if segment in ("articles", "tags", "callback"):
        return settings.content_service_url
    if segment == "distill":
        return settings.ai_service_url
    return None


@app.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_fallback(request: Request, path: str):
    """路由表没命中的兜底：按第一段路径猜下游（CP1.4/1.5 行为，逐步被路由表取代）。"""
    target_base = _resolve_target(path)
    if target_base is None:
        return Response(status_code=404, content=b'{"detail":"no downstream route"}')

    url = f"{target_base}/api/v1/{path}"
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }

    client = request.app.state.httpx
    upstream = await client.request(
        request.method,
        url,
        params=request.query_params,
        content=body,
        headers=headers,
        timeout=30.0,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
