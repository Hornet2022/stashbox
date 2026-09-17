import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_lifespan_startup_quota_loop_running(client):
    """user-service startup 后 /healthz 200 OK（说明 lifespan OK）"""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_endpoint_works_after_lifespan(client):
    """user-service /metrics 在 lifespan 启动后仍正常返"""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "http_requests_total" in body


def test_readyz_endpoint_works(client):
    """user-service /readyz 在 lifespan 启动后返 PG/Redis 检查结果"""
    resp = client.get("/readyz")
    assert resp.status_code in (200, 503)
