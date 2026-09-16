"""ASGI middleware（CP6.4-pre）：注入 X-Request-ID + 自动埋 Prometheus metrics。

- X-Request-ID：请求头带就沿用（api-gateway → 下游串联），否则生成 `req_xxx`
- 响应头回写 X-Request-ID，客户端可拿去做链路排查
- 每个请求记 3 个指标：REQUEST_COUNT / REQUEST_LATENCY / ERROR_COUNT(仅 >= 400)
"""
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Match

from stashbox.backend.common.logging import bind_request_id, get_logger, new_request_id
from stashbox.backend.common.observability import (
    ERROR_COUNT,
    REQUEST_COUNT,
    REQUEST_LATENCY,
)

log = get_logger("middleware")


def endpoint_label(request: Request) -> str:
    """路由模板（`/api/v1/articles/{article_id}`）而非真实路径 —— 否则 label 基数爆炸。

    Starlette < 1.6 会把匹配到的 route 放进 scope["route"]；1.6 起不再放，
    故这里自己按路由表回查（命中不了就退化成真实路径，如 404）。
    """
    route = request.scope.get("route")
    if route is not None:
        return getattr(route, "path", request.url.path)

    for candidate in request.app.routes:
        match, _child_scope = candidate.matches(request.scope)
        if match == Match.FULL:
            return getattr(candidate, "path", request.url.path)
    return request.url.path


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        upstream_rid = request.headers.get("X-Request-ID")
        rid = upstream_rid or new_request_id()
        if upstream_rid:
            bind_request_id(upstream_rid)  # 上游传来的 id：接住，别再生成一个
        request.state.request_id = rid

        endpoint = endpoint_label(request)
        service = request.app.title

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                error=str(e),
                request_id=rid,
            )
            ERROR_COUNT.labels(service=service, endpoint=endpoint, status=500).inc()
            raise
        duration = time.perf_counter() - start

        status = response.status_code
        REQUEST_COUNT.labels(
            service=service, method=request.method, endpoint=endpoint, status=status
        ).inc()
        REQUEST_LATENCY.labels(service=service, endpoint=endpoint).observe(duration)
        if status >= 400:
            ERROR_COUNT.labels(service=service, endpoint=endpoint, status=status).inc()

        response.headers["X-Request-ID"] = rid
        log.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=status,
            duration_ms=round(duration * 1000, 2),
            request_id=rid,
        )
        return response
