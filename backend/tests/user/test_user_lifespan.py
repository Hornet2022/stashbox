import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """服务目录名带连字符（user-service），不能当包 import，按文件路径加载。

    本文件原先写的是 `from main import app` —— 只有在 `user-service/` 恰好在
    sys.path 上时才成立，从 `backend/` 跑 pytest 时不成立，收集期直接
    `ModuleNotFoundError: No module named 'main'`。

    这正是 CI 里那条 `--ignore=tests/user/test_user_lifespan.py` 的由来。
    同目录的 test_notifications / test_onboarding / test_quota_reset_loop
    早就统一用这个 _load_app 写法了，只有本文件漏改。
    """
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


app = _load_app("_lifespan_user_main", "user-service/main.py")


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
