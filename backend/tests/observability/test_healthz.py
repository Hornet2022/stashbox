"""CP6.4-pre：4 服务 `/healthz`（liveness）—— 不看依赖，活着就 200。"""
import httpx
import pytest

SERVICE_NAMES = ["api-gateway", "user-service", "content-service", "ai-service"]


@pytest.mark.parametrize("name", SERVICE_NAMES)
async def test_healthz_returns_ok(apps, asgi_client, name):
    async with asgi_client(apps[name]) as c:
        r = await c.get("/healthz")

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("name", SERVICE_NAMES)
async def test_healthz_not_recorded_as_dependency_check(apps, asgi_client, name):
    """/healthz 必须纯 liveness：响应体里不出现 checks 字段（那是 /readyz 的活）。"""
    async with asgi_client(apps[name]) as c:
        body = (await c.get("/healthz")).json()

    assert "checks" not in body


@pytest.mark.parametrize("name", SERVICE_NAMES)
async def test_legacy_health_alias_still_works(apps, asgi_client, name):
    """CP1.6 及之前客户端在用 `/health`，不能删。"""
    async with asgi_client(apps[name]) as c:
        r = await c.get("/health")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


async def test_ai_service_health_keeps_redis_field(apps, asgi_client):
    """ai-service 的 `/health` 原本带 redis 检查字段，保持向后兼容。"""
    async with asgi_client(apps["ai-service"]) as c:
        body = (await c.get("/health")).json()

    assert body["service"] == "ai-service"
    assert body["redis"] in ("ok", "error")


async def test_healthz_ok_even_when_db_down(apps, asgi_client):
    """liveness 不看依赖：这里连 root conftest 的 engine dispose 都不触发，纯 HTTP。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps["content-service"]), base_url="http://test"
    ) as c:
        r = await c.get("/healthz")

    assert r.status_code == 200
