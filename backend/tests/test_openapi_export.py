"""CP6.6：OpenAPI 3.1 schema 导出 + 合并 的测试。

覆盖：
  1. 4 个服务都能导出 openapi（不需要起 server / 不碰 DB）
  2. 合并 schema 是合法 OpenAPI 3.1（openapi-spec-validator）
  3. 合并 schema 含 4 服务全部端点 + 4 个 tag
  4. 合并 schema 含 admin API 7 端点

不需要 PG/Redis：``app.openapi()`` 只遍历路由表 + pydantic 模型。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from openapi_spec_validator import validate as validate_openapi

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "cp66_export_openapi", SCRIPTS_DIR / "export_openapi.py"
)
export_openapi = importlib.util.module_from_spec(_spec)
sys.modules["cp66_export_openapi"] = export_openapi
_spec.loader.exec_module(export_openapi)

HTTP_METHODS = export_openapi.HTTP_METHODS

# admin API 7 端点（path 里的参数名用 {} 归一，避免耦合 {user_id}/{article_id} 命名）
ADMIN_ENDPOINTS = {
    "/api/v1/admin/stats": "get",
    "/api/v1/admin/users": "get",
    "/api/v1/admin/users/{}/quota-adjust": "post",
    "/api/v1/admin/articles/{}/force-retry": "post",
    "/api/v1/admin/audio/{}/invalidate": "post",
    "/api/v1/admin/audit-log": "get",
    "/api/v1/admin/auth/login": "post",
}


def _norm_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


# FastAPI 生成的 ValidationError schema 里 input / ctx 两个字段随版本变化
# （0.141.x 有、0.115.x 没有）。pyproject 钉的是 fastapi = "^0.115.0"，
# 而本机 dev venv 实际装的是 0.141.x —— 直接比整个 dict 会因这两个字段
# 误报「落盘 schema 不一致」。该断言的本意是「端点 / tag / admin 没漂移」，
# 所以比对前先摘掉这两个版本相关字段（落盘文件本身仍按原样做合法性校验）。
_VERSION_VARIANT_FIELDS = ("input", "ctx")


def _drop_version_variant_fields(schema: dict) -> dict:
    props = (
        schema.get("components", {})
        .get("schemas", {})
        .get("ValidationError", {})
        .get("properties")
    )
    if isinstance(props, dict):
        for field in _VERSION_VARIANT_FIELDS:
            props.pop(field, None)
    return schema


def _endpoints(schema: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, item in (schema.get("paths") or {}).items():
        for key in item:
            if key in HTTP_METHODS:
                out.add((_norm_path(path), key.upper()))
    return out


@pytest.fixture(scope="module")
def service_schemas() -> dict[str, dict]:
    return export_openapi.export_service_schemas()


@pytest.fixture(scope="module")
def merged(service_schemas) -> dict:
    return export_openapi.build_merged_schema(service_schemas)


def test_all_four_services_export_openapi(service_schemas):
    """4 个服务都能导出 openapi.json，且各自有端点。"""
    assert set(service_schemas) == {
        "api-gateway",
        "user-service",
        "content-service",
        "ai-service",
    }
    for service, schema in service_schemas.items():
        assert schema.get("openapi", "").startswith("3.1"), f"{service} 不是 OpenAPI 3.1"
        assert schema.get("paths"), f"{service} 没有导出任何 path"


def test_merged_schema_is_valid_openapi_31(merged):
    """合并 schema 通过 openapi-spec-validator（OpenAPI 3.1）。"""
    assert merged["openapi"].startswith("3.1")
    validate_openapi(merged)  # 不合法会抛异常


def test_merged_schema_contains_all_endpoints_and_tags(merged, service_schemas):
    """合并后端点 = 各服务端点的并集（不丢端点），且含 4 个服务 tag。"""
    expected = set()
    for schema in service_schemas.values():
        expected |= _endpoints(schema)
    actual = _endpoints(merged)
    assert actual == expected, f"丢了端点：{sorted(expected - actual)}"
    assert {t["name"] for t in merged["tags"]} == {
        "api-gateway",
        "user-service",
        "content-service",
        "ai-service",
    }


def test_merged_schema_contains_admin_endpoints(merged):
    """admin API 7 端点全部在合并 schema 里（path + method 都要对）。"""
    actual = _endpoints(merged)
    missing = [
        (path, method.upper())
        for path, method in ADMIN_ENDPOINTS.items()
        if (path, method.upper()) not in actual
    ]
    assert not missing, f"缺 admin 端点：{missing}"


def test_generated_file_matches_and_is_valid():
    """落盘的 stashbox-openapi.json（若有）是合法 OpenAPI 3.1 且与现场生成一致。"""
    out_path = export_openapi.OUT_PATH
    if not out_path.is_file():
        pytest.skip(f"{out_path} 不存在（未跑 export_openapi.py）")
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    validate_openapi(on_disk)
    fresh = export_openapi.build_merged_schema(export_openapi.export_service_schemas())
    assert _drop_version_variant_fields(on_disk) == _drop_version_variant_fields(fresh), (
        "落盘 schema 与现场生成不一致，请重跑 export_openapi.py"
    )
