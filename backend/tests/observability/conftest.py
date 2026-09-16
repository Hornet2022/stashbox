"""CP6.4-pre 可观测性单测 fixture：4 服务 app 加载 + structlog 复位。

4 个服务目录名都带连字符，不能直接 import，按文件路径加载
（做法同 tests/content/helpers.py）。
"""
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from stashbox.backend.common import logging as common_logging

BACKEND_DIR = Path(__file__).resolve().parents[2]

SERVICES: dict[str, tuple[str, str]] = {
    "api-gateway": ("_cp64_api_gateway_main", "api-gateway/main.py"),
    "user-service": ("_cp64_user_service_main", "user-service/main.py"),
    "content-service": ("_cp64_content_service_main", "content-service/main.py"),
    "ai-service": ("_cp64_ai_service_main", "ai-service/main.py"),
}


def _load_app(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def apps():
    """4 服务 FastAPI app（session 级单例，避免重复 exec module）。"""
    return {name: _load_app(mod, rel).app for name, (mod, rel) in SERVICES.items()}


@pytest.fixture(scope="session")
def content_app(apps):
    return apps["content-service"]


@pytest.fixture
def asgi_client():
    """造 httpx ASGI 客户端的工厂（headers 下划线转连字符）。"""

    def _make(app, **extra_headers):
        headers = {k.replace("_", "-"): v for k, v in extra_headers.items()}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        )

    return _make


@pytest.fixture(autouse=True)
def _reset_logging():
    """每个 case 前都重配 structlog（保证 JSON renderer），case 后清 contextvars。

    service / request_id 走 contextvars，不清理会串到下一个 case。
    """
    common_logging.setup_logging("pytest")
    yield
    common_logging.clear_context()
