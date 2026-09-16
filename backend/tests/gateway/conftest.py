"""CP1.7.1 api-gateway 单测 fixture：gateway app + in-process 上游。

要点：
  - gateway 与 content-service 都「按文件加载」（目录名带连字符），做法同 tests/content
  - 上游走 httpx ASGITransport 注入 gateway 的 httpx client —— 不发真实 HTTP，
    但跑的是真实 content-service 代码，403/404/ready 这些语义是真的
  - 需要造 5xx / 固定响应时，用 stub_app() 自己 mount 一个替身覆盖默认上游
  - 上游收到的请求记在 upstream_requests 里（httpx event_hooks）
"""
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

BACKEND_DIR = Path(__file__).resolve().parents[2]

# 复用 tests/content 的 DB helper（new_user / new_article / new_task …）——
# 它同时把 content-service main.py 加载成模块，正好当真实上游用。
sys.path.insert(0, str(BACKEND_DIR / "tests" / "content"))  # noqa: E402

import helpers  # noqa: E402


def _load_app(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def gateway_main():
    return _load_app("_cp171_gateway_main", "api-gateway/main.py")


@pytest.fixture(scope="session")
def gateway_app(gateway_main):
    return gateway_main.app


@pytest.fixture(scope="session")
def content_app():
    """真实 content-service app（helpers 已加载）当上游。"""
    return helpers.content_main.app


@pytest.fixture(scope="session")
def content_base(gateway_main) -> str:
    """路由表里 content-service 的 base url（env 可覆盖，别写死）。"""
    return gateway_main.D9_ROUTE.target_url


@pytest.fixture(autouse=True)
def fake_ai_client(monkeypatch) -> helpers.FakeAIClient:
    """不真调 ai-service（单测环境没有 8103 在跑）。"""
    fake = helpers.FakeAIClient()
    monkeypatch.setattr(helpers.content_main, "get_ai_client", lambda: fake)
    return fake


@pytest.fixture
def upstream_requests() -> list[httpx.Request]:
    """上游收到的请求（用于断言转发内容：path / header / body）。"""
    return []


@pytest.fixture
async def mount(gateway_app, content_base, upstream_requests):
    """把某个 app 挂成 gateway 的上游，返回挂上去的 client。"""

    clients: list[httpx.AsyncClient] = []

    async def _record(request: httpx.Request) -> None:
        upstream_requests.append(request)

    def _mount(app) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=content_base,
            event_hooks={"request": [_record]},
        )
        gateway_app.state.httpx = client  # 单测没有 lifespan，手动塞
        clients.append(client)
        return client

    yield _mount
    for client in clients:
        await client.aclose()
    gateway_app.state.httpx = None


@pytest.fixture(autouse=True)
def upstream(mount, content_app) -> httpx.AsyncClient:
    """默认上游 = 真实 content-service；要造 5xx 的 case 自己再 mount 一次覆盖。"""
    return mount(content_app)


@pytest.fixture
async def gw(gateway_app):
    """打 gateway 用的 ASGI 客户端（headers 每次请求自己带）。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway"
    ) as client:
        yield client


@pytest.fixture
def stub_app():
    """造一个只返回固定响应的上游替身（5xx / 自定义 body 场景）。"""

    def _make(status: int = 200, payload: dict | None = None) -> FastAPI:
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def _stub():
            return JSONResponse(content=payload if payload is not None else {}, status_code=status)

        return app

    return _make
