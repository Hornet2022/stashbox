"""CP6.4-pre：`/metrics` Prometheus 抓取端点（text/plain 0.0.4 + 3 个指标族）。"""
import sys
from pathlib import Path

# 复用 content 单测的 helper（造用户 / ASGI client），不重复实现一遍
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "content"))

from helpers import client, new_user  # noqa: E402  (tests/content/helpers.py)

CONTENT_SERVICE = "stashbox-content-service"


def _samples(body: str, family: str, service: str = CONTENT_SERVICE) -> list[str]:
    """按 指标族 + service label 过滤样本行（跳过 # HELP / # TYPE 注释）。"""
    return [
        line
        for line in body.splitlines()
        if line.startswith(family) and f'service="{service}"' in line
    ]


def _value(line: str) -> float:
    return float(line.rsplit(" ", 1)[1])


async def test_metrics_is_prometheus_text(content_app, asgi_client):
    async with asgi_client(content_app) as c:
        await c.get("/healthz")
        r = await c.get("/metrics")

    assert r.status_code == 200
    content_type = r.headers["content-type"]
    assert content_type.startswith("text/plain")
    assert "version=" in content_type  # prometheus text 0.0.4（新版 client 可能是 1.0.0）
    body = r.text
    assert "# HELP http_requests_total" in body
    assert "# TYPE http_requests_total counter" in body


async def test_metrics_exposes_all_three_families(content_app, asgi_client):
    async with asgi_client(content_app) as c:
        await c.get("/healthz")
        await c.get("/api/v1/articles/art_not_exist")  # 401（无 JWT）→ 进 ERROR_COUNT
        body = (await c.get("/metrics")).text

    assert _samples(body, "http_requests_total")
    assert _samples(body, "http_request_duration_seconds_bucket")
    assert _samples(body, "http_request_errors_total")


async def test_post_article_is_counted(content_app, asgi_client):
    """§4.3：请求 POST /api/v1/articles 后，对应 endpoint + method 的计数 >= 1。"""
    _uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.post("/api/v1/articles", json={"url": "https://example.com/a"})
        assert r.status_code == 200, r.text

    async with asgi_client(content_app) as c:
        body = (await c.get("/metrics")).text

    lines = [
        line
        for line in _samples(body, "http_requests_total")
        if 'endpoint="/api/v1/articles"' in line
        and 'method="POST"' in line
        and 'status="200"' in line
    ]
    assert lines, body
    assert _value(lines[0]) >= 1

    # 同一个 endpoint 也要有延迟直方图观测
    buckets = [
        line
        for line in _samples(body, "http_request_duration_seconds_bucket")
        if 'endpoint="/api/v1/articles"' in line
    ]
    assert buckets, body


async def test_4xx_is_counted_as_error(content_app, asgi_client):
    _uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        r = await c.get("/api/v1/articles/art_definitely_missing")
        assert r.status_code == 404, r.text

    async with asgi_client(content_app) as c:
        body = (await c.get("/metrics")).text

    lines = [
        line
        for line in _samples(body, "http_request_errors_total")
        if 'endpoint="/api/v1/articles/{article_id}"' in line and 'status="404"' in line
    ]
    assert lines, body
    assert _value(lines[0]) >= 1


async def test_metrics_labels_use_route_template(content_app, asgi_client):
    """label 用路由模板（/api/v1/articles/{article_id}）而非真实 id，避免基数爆炸。"""
    _uid, token = await new_user(monthly_quota=5)

    async with client(token) as c:
        assert (await c.get("/api/v1/articles/art_a1b2c3")).status_code == 404

    async with asgi_client(content_app) as c:
        body = (await c.get("/metrics")).text

    assert "/api/v1/articles/art_a1b2c3" not in body
    assert "/api/v1/articles/{article_id}" in body
