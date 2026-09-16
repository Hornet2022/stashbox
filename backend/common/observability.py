"""
可观测性通用模块（CP6.4-pre）：Prometheus metrics + healthz/readyz。

约定（k8s 语义）：
  - GET /healthz  liveness  —— 进程活着就 200，不看依赖
  - GET /readyz   readiness —— PG + Redis 都通才 200，否则 503
  - GET /metrics  Prometheus scrape 端点（text/plain 0.0.4）
"""
import redis.asyncio as redis
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.database import get_db
from stashbox.backend.common.redis_client import get_redis_pool

# ---- Prometheus metrics ----
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["service", "method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ERROR_COUNT = Counter(
    "http_request_errors_total",
    "HTTP request errors (4xx + 5xx)",
    ["service", "endpoint", "status"],
)

def metrics_endpoint() -> Response:
    """Prometheus 抓取端点。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def healthz() -> dict:
    """Liveness probe：进程活着即 ok，不碰任何依赖。"""
    return {"status": "ok"}


async def check_readyz(db: AsyncSession) -> dict:
    """Readiness 检查本体（不依赖 FastAPI Request，便于单测直调）。"""
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["pg"] = "ok"
    except Exception as e:  # noqa: BLE001 - probe 要吞掉所有异常并转成状态码
        checks["pg"] = f"error: {e}"

    checks["redis"] = await _ping_redis()

    status = "ok" if all(v == "ok" for v in checks.values()) else "error"
    return {"status": status, "checks": checks}


async def _ping_redis() -> str:
    try:
        client = redis.Redis(connection_pool=get_redis_pool())
        try:
            await client.ping()
        finally:
            await client.aclose()
    except Exception as e:  # noqa: BLE001 - 同上
        return f"error: {e}"
    return "ok"


async def readyz(db: AsyncSession = Depends(get_db)) -> Response:
    """Readiness probe：PG + Redis 全通 → 200，否则 503。"""
    payload = await check_readyz(db)
    status_code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(content=payload, status_code=status_code)


def install_health_endpoints(app: FastAPI) -> None:
    """统一挂 /healthz /readyz /metrics 三个端点。"""
    app.add_api_route("/healthz", healthz, methods=["GET"], tags=["health"])
    app.add_api_route("/readyz", readyz, methods=["GET"], tags=["health"])
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], tags=["health"])


def install_middleware(app: FastAPI) -> None:
    """安装 RequestID + metrics 埋点中间件。

    Starlette middleware 是栈式 LIFO：本函数在 app 路由注册完后调用，挂在外层，
    保证 request_id 在业务处理之前就注入 structlog context。
    metrics 的 service label 取 app.title（如 "stashbox-content-service"）。
    """
    from stashbox.backend.common.middleware import RequestIDMiddleware

    app.add_middleware(RequestIDMiddleware)
