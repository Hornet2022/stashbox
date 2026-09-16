"""
api-gateway（端口 8000） - 听匣统一入口 + JWT 签发 + 路由分发。

职责：
  - GET  /health                                 健康检查
  - POST /api/v1/auth/token                     签发 JWT（公开）
  - /api/v1/{path:path}                          按前缀转发到下游 3 个服务

下游路由（见 stashbox.backend.common.config.Settings）：
  /api/v1/user        / /api/v1/subscription  -> user_service_url
  /api/v1/articles    / /api/v1/tags /callback -> content_service_url
  /api/v1/distill     / /api/v1/admin/distill  -> ai_service_url
  未匹配                                              -> 404
"""
from contextlib import asynccontextmanager

# 注意：本服务不再使用 sys.path hack。stashbox 包通过 PYTHONPATH（见 run_dev.sh）
# 或 `pip install -e` 导入。直接 `uvicorn main:app` 时需保证仓库根父目录在 PYTHONPATH 中。

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import register_exception_handlers
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.observability import install_health_endpoints

setup_logging("api-gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.httpx = httpx.AsyncClient()
    yield
    await app.state.httpx.aclose()


app = FastAPI(title="stashbox-api-gateway", version="0.1.0", lifespan=lifespan)
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)


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


def _resolve_target(path: str) -> str | None:
    """根据第一段路径决定下游 base url。"""
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
async def proxy(request: Request, path: str):
    target_base = _resolve_target(path)
    if target_base is None:
        return Response(status_code=404, content=b'{"detail":"no downstream route"}')

    url = f"{target_base}/api/v1/{path}"
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
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
