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
def modules():
    """4 服务模块对象（session 级单例，避免重复 exec module）。

    与 `apps` 并存而不是只留 app：有些用例要 patch 服务模块里的符号
    （如 content-service 的 `get_fetcher`），那些符号挂在模块上、不在
    FastAPI app 对象上。
    """
    return {name: _load_app(mod, rel) for name, (mod, rel) in SERVICES.items()}


@pytest.fixture(scope="session")
def apps(modules):
    """4 服务 FastAPI app。"""
    return {name: module.app for name, module in modules.items()}


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


@pytest.fixture
def fake_capture_fetch(monkeypatch):
    """把剪藏的抓取层换掉，让「建文章」不依赖真实 DNS 与外网。

    原用例打 `https://example.com/a` 指望它能抓下来。但 10-03 补的 SSRF 守卫
    会解析目标地址并拦下回环/内网 —— 而 `example.com` 在不少环境（沙箱、
    企业 split-DNS）正好解析到 127.0.0.1，于是用例返回 400「暂不支持这个
    链接来源」。

    守卫本身是对的（实测它正确拦住了环回地址），脆的是用例：这个测试要验的
    是 **metrics 计数**，跟能不能真的抓到网页毫无关系，不该被 DNS 牵着走。
    这里按 content 目录 D9 用例的同一套做法 mock `get_fetcher`。

    注意必须 patch `helpers.content_main` 而不是本文件 `modules` 里的那一份：
    本目录 conftest 自己按文件路径加载了一份 content-service/main.py，而
    `client()` 来自 tests/content/helpers.py，加载的是**另一个**模块实例 ——
    两个 app 各挂各的模块级符号，patch 错对象会静默无效。
    """
    from fetchers.base import FetchResult

    from helpers import content_main as helpers_content_main

    class _FakeFetcher:
        async def fetch(self, url: str, timeout=None, budget=None):
            return FetchResult(
                url=url,
                title="测试文章",
                content_html="<p>正文</p>",
                content_text="测试正文",
                source="generic_url",
            )

    monkeypatch.setattr(helpers_content_main, "get_fetcher", lambda _url: _FakeFetcher())


@pytest.fixture(autouse=True)
def _reset_logging():
    """每个 case 前都重配 structlog（保证 JSON renderer），case 后清 contextvars。

    service / request_id 走 contextvars，不清理会串到下一个 case。
    """
    common_logging.setup_logging("pytest")
    yield
    common_logging.clear_context()
