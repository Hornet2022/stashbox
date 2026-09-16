"""CP6.4-pre：4 服务 `/readyz`（readiness）—— PG + Redis 全通才 200，否则 503。"""
import pytest

from stashbox.backend.common import observability
from stashbox.backend.common.database import get_db


class BrokenSession:
    """PG 不可用的替身：任何 SQL 都抛错。"""

    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("connection refused")


async def _broken_db():
    yield BrokenSession()


@pytest.fixture
def broken_pg(content_app):
    content_app.dependency_overrides[get_db] = _broken_db
    yield
    content_app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "name", ["api-gateway", "user-service", "content-service", "ai-service"]
)
async def test_readyz_ok_when_dependencies_up(apps, asgi_client, name):
    async with asgi_client(apps[name]) as c:
        r = await c.get("/readyz")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"pg": "ok", "redis": "ok"}


async def test_readyz_returns_503_when_pg_down(content_app, asgi_client, broken_pg):
    async with asgi_client(content_app) as c:
        r = await c.get("/readyz")

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "error"
    assert body["checks"]["pg"].startswith("error:")
    assert body["checks"]["redis"] == "ok"


async def test_readyz_returns_503_when_redis_down(content_app, asgi_client, monkeypatch):
    async def _redis_down() -> str:
        return "error: connection refused"

    monkeypatch.setattr(observability, "_ping_redis", _redis_down)

    async with asgi_client(content_app) as c:
        r = await c.get("/readyz")

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "error"
    assert body["checks"]["pg"] == "ok"
    assert body["checks"]["redis"] == "error: connection refused"


async def test_readyz_returns_503_when_both_down(content_app, asgi_client, broken_pg, monkeypatch):
    async def _redis_down() -> str:
        return "error: connection refused"

    monkeypatch.setattr(observability, "_ping_redis", _redis_down)

    async with asgi_client(content_app) as c:
        r = await c.get("/readyz")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["checks"]["pg"].startswith("error:")
    assert body["checks"]["redis"] == "error: connection refused"


async def test_check_readyz_unit_level():
    """绕过 HTTP，直接测 readiness 检查本体（便于注入任意 session）。"""
    payload = await observability.check_readyz(BrokenSession())
    assert payload["status"] == "error"
    assert payload["checks"]["pg"] == "error: connection refused"
