"""网关 CORS 白名单回归（2026-10-21）

修前的兜底是 ``allow_origins=["*"]`` + ``allow_credentials=True``。
两者同时成立时，等价于**任何网站**都能带着运营后台的 cookie 调本网关的
``/api/v1/admin/*`` —— admin-web 是 cookie 优先鉴权
（见 stashbox-admin-web/src/api/client.ts），所以这不是理论风险。

另外 "*" + credentials 本身就不是合法的 CORS 响应头组合
（浏览器要求带 credentials 时 Access-Control-Allow-Origin 不能是 "*"），
实际是靠 Starlette 在带 cookie 时回显 Origin 才凑合能用 —— 能用不等于对。

修后的口径：
  - 配了 STASHBOX_CORS_ALLOW_ORIGINS → 用它
  - prod 且没配 → **启动即失败**（不给"生产环境忘了配"留机会）
  - dev/staging 且没配 → 只放行本机开发端口
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from starlette.middleware.cors import CORSMiddleware

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _cors_middleware(app):
    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            return m
    return None


# ---------------------------------------------------------------------------
# 1. dev 下不允许通配符
# ---------------------------------------------------------------------------
def test_dev_cors_is_not_wildcard(gateway_app):
    """dev 兜底也必须是具体 origin 列表，不是 "*"。"""
    cors = _cors_middleware(gateway_app)
    assert cors is not None, "CORS 中间件没装上"
    origins = cors.kwargs.get("allow_origins", [])
    assert "*" not in origins, f"dev 也不该放行全网：{origins}"
    assert origins, "dev 至少要放行本机 admin-web dev 端口"
    assert any("5173" in o for o in origins), f"得包含 admin-web dev 端口：{origins}"


# ---------------------------------------------------------------------------
# 2. 显式配置时用配置的
# ---------------------------------------------------------------------------
def test_explicit_allowlist_wins(monkeypatch):
    monkeypatch.setenv("STASHBOX_CORS_ALLOW_ORIGINS", "https://a.example.com,https://b.example.com")
    module = _load_app("_cors_guard_explicit", "api-gateway/main.py")
    cors = _cors_middleware(module.app)
    assert cors is not None
    assert cors.kwargs.get("allow_origins") == ["https://a.example.com", "https://b.example.com"]


# ---------------------------------------------------------------------------
# 3. prod 下没配必须启动失败
# ---------------------------------------------------------------------------
def test_prod_without_allowlist_fails_fast(monkeypatch):
    """生产忘了配白名单 → 启动就失败，而不是默默放行全网。

    这个仓库已经有同类的"生产必须显式配置"惯例：
    common/config.py 的 jwt_secret 断言、安卓的 release signing 门禁。
    """
    from stashbox.backend.common.config import settings

    monkeypatch.delenv("STASHBOX_CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.setattr(settings, "environment", "prod")

    with pytest.raises(RuntimeError, match="STASHBOX_CORS_ALLOW_ORIGINS"):
        _load_app("_cors_guard_prod", "api-gateway/main.py")


# ---------------------------------------------------------------------------
# 4. prod 下配了就正常启动
# ---------------------------------------------------------------------------
def test_prod_with_allowlist_starts(monkeypatch):
    from stashbox.backend.common.config import settings

    monkeypatch.setenv("STASHBOX_CORS_ALLOW_ORIGINS", "https://stashbox.cn")
    monkeypatch.setattr(settings, "environment", "prod")

    module = _load_app("_cors_guard_prod_ok", "api-gateway/main.py")
    cors = _cors_middleware(module.app)
    assert cors is not None
    assert cors.kwargs.get("allow_origins") == ["https://stashbox.cn"]
