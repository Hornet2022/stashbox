#!/usr/bin/env python3
"""CP6.6：导出 4 个服务的 OpenAPI 3.1 schema 并合并成一份文档 schema。

**不启动 server**：直接用 importlib 把每个服务的 ``main.py`` 按文件加载，
取 ``module.app.openapi()`` 的返回值。FastAPI 的 ``openapi()`` 是纯函数式
生成（遍历路由表 + pydantic 模型），不碰网络 / DB / 生命周期，所以无需
uvicorn，也无需 4 个端口在跑。

为什么要 importlib：服务目录名带连字符（``api-gateway`` / ``user-service``
/ ``content-service`` / ``ai-service``）不是合法 Python 包名，无法 ``import``，
只能 ``spec_from_file_location`` 按路径加载（做法同 backend/tests/content/helpers.py）。

用法::

    python backend/scripts/export_openapi.py            # 写 backend/docs/openapi/stashbox-openapi.json
    python backend/scripts/export_openapi.py --stdout   # 只打印到 stdout，不落盘
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/scripts/ -> 仓库根
BACKEND_DIR = REPO_ROOT / "backend"
REPO_PARENT = REPO_ROOT.parent  # stashbox 包（= 仓库根）所在目录

# stashbox 包从仓库根父目录导入；ai-service 的顶层模块（dispatcher 等）需其自身目录在 path。
for _p in (str(REPO_PARENT), str(BACKEND_DIR / "ai-service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# (service tag, 相对 backend/ 的 main.py 路径)
SERVICES: list[tuple[str, str]] = [
    ("api-gateway", "api-gateway/main.py"),
    ("user-service", "user-service/main.py"),
    ("content-service", "content-service/main.py"),
    ("ai-service", "ai-service/main.py"),
]

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

OUT_PATH = BACKEND_DIR / "docs" / "openapi" / "stashbox-openapi.json"


def load_app(service: str, rel: str):
    """按文件加载某个服务的 ``main.py``，返回其 FastAPI ``app``。"""
    module_name = "_stashbox_openapi_" + service.replace("-", "_")
    path = BACKEND_DIR / rel
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写死，正常不会触发
        raise RuntimeError(f"无法加载服务模块：{path}")
    module = importlib.util.module_from_spec(spec)
    # 先登记进 sys.modules，避免 main.py 内部相对 import / 循环引用踩坑
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.app


def export_service_schemas() -> dict[str, dict]:
    """返回 ``{service: openapi_schema}``（每个服务各调一次 ``app.openapi()``）。"""
    schemas: dict[str, dict] = {}
    for service, rel in SERVICES:
        schemas[service] = load_app(service, rel).openapi()
    return schemas


def _tag_operation(op: dict, service: str) -> None:
    """给 operation 打上 service tag（保留服务自身原有的 tags）。"""
    tags = list(op.get("tags") or [])
    if service not in tags:
        tags.insert(0, service)
    op["tags"] = tags


PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")


def _synthetic_path_param(name: str) -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }


def _normalize_path_params(path: str, item: dict) -> None:
    """补齐 path 模板变量对应的 ``in: path`` 参数声明。

    api-gateway 的动态代理路由（``add_api_route(route.path, proxy)``）里
    路径模板带 ``{article_id}`` 但 handler 不接收该参数，FastAPI 生成的
    operation 就缺 ``parameters`` —— 这种 schema 不合法。这里按路径模板
    补一份 string 类型声明，只修文档 schema，不动任何业务代码。
    """
    names = PATH_PARAM_RE.findall(path)
    if not names:
        return
    path_level = {
        p.get("name") for p in item.get("parameters") or [] if p.get("in") == "path"
    }
    operations = [
        (key, value)
        for key, value in item.items()
        if key in HTTP_METHODS and isinstance(value, dict)
    ]
    for name in names:
        if name in path_level:
            continue
        lacking = []
        for _key, op in operations:
            declared = {
                p.get("name") for p in op.get("parameters") or [] if p.get("in") == "path"
            }
            if name not in declared:
                lacking.append(op)
        if not lacking:
            continue
        if len(lacking) == len(operations):
            # 所有 operation 都没声明 → 提到 path 级，一处覆盖全部
            item.setdefault("parameters", []).append(_synthetic_path_param(name))
        else:
            for op in lacking:
                op.setdefault("parameters", []).append(_synthetic_path_param(name))


# 规范化输出用的固定 method 顺序（FastAPI 内部把 methods 存 set，迭代顺序不稳定）
METHOD_ORDER = ["get", "put", "post", "delete", "options", "head", "patch", "trace"]


def _ordered_methods(item: dict) -> list[str]:
    return sorted((k for k in item if k in HTTP_METHODS), key=METHOD_ORDER.index)


def _finalize_path_items(merged: dict) -> None:
    """把每个 path item 的 key 排成稳定顺序（method 固定序 + 其余保持）。

    目的：让生成的 JSON 逐字节可复现 —— 否则 FastAPI 用 set 迭代 method，
    同一份代码两次导出的 operationId 后缀 / key 顺序会漂移，git diff 全是噪声，
    schema diff 脚本也会误报。
    """
    for path, item in merged["paths"].items():
        ordered: dict = {}
        for method in _ordered_methods(item):
            ordered[method] = item[method]
        for key, value in item.items():
            if key not in ordered:
                ordered[key] = value
        merged["paths"][path] = ordered


def _operation_id_from(method: str, path: str) -> str:
    """由 method + path 生成稳定的 operationId（如 ``get_api_v1_articles_article_id``）。"""
    return re.sub(r"[^0-9a-zA-Z]+", "_", f"{method}_{path}").strip("_")


def _normalize_operation_ids(merged: dict) -> None:
    """保证 operationId 全局唯一（OpenAPI 硬要求），且结果与进程无关。

    api-gateway 的兜底代理把同一个 handler ``proxy_fallback`` 注册到
    ``/api/v1/{path}`` 的 5 个 method 上，FastAPI 给这 5 个 operation 生成了
    **完全相同**的 operationId；而且它挑哪个 method 当后缀取决于 set 迭代顺序
    （跨进程不稳定）。所以：唯一的 id 原样保留；**重复的**改成由 method+path
    派生的稳定 id。先统计再赋值，不依赖遍历顺序。
    """
    occurrences: dict[str, int] = {}
    entries: list[tuple[dict, str, str, str]] = []
    for path in sorted(merged["paths"]):
        item = merged["paths"][path]
        for key in _ordered_methods(item):
            op = item[key]
            if not isinstance(op, dict):
                continue
            oid = op.get("operationId") or _operation_id_from(key, path)
            occurrences[oid] = occurrences.get(oid, 0) + 1
            entries.append((op, key, path, oid))

    seen: set[str] = set()
    for op, key, path, oid in entries:
        if occurrences[oid] == 1:
            candidate = oid
        else:
            candidate = _operation_id_from(key, path)
        base = candidate
        counter = 1
        while candidate in seen:
            counter += 1
            candidate = f"{base}_{counter}"
        op["operationId"] = candidate
        seen.add(candidate)


def build_merged_schema(schemas: dict[str, dict]) -> dict:
    """把 4 份 schema 合并成一份合法 OpenAPI 3.1。

    合并规则：
      - ``paths``：按 path 合并；同一 path 的不同 method 各自保留。
        若同一 ``(path, method)`` 在多个服务都出现，**以下游服务定义为准**
        （api-gateway 只是代理转发，schema 里没有参数/响应模型；下游才是
        真实定义），并把所有来源服务并进 tag。这样既拿到信息量最大的定义，
        又不丢「这个端点 gateway 也暴露」的事实。
      - 合并后补齐 path 模板变量缺的参数声明（见 ``_normalize_path_params``）。
      - ``components.schemas`` / ``securitySchemes``：按名字取并集（同名先到先得）。
      - 顶层 ``tags``：登记 4 个服务，供 Redoc 左侧分组展示。
    """
    merged: dict = {
        "openapi": "3.1.0",
        "info": {
            "title": "Stashbox API",
            "version": "0.1.0",
            "description": (
                "听匣（Stashbox）后端 4 服务合并 OpenAPI schema。\n\n"
                "由 `backend/scripts/export_openapi.py` 自动生成，请勿手改。\n\n"
                "服务分组见 tags：api-gateway / user-service / content-service / ai-service。"
            ),
        },
        "tags": [],
        "paths": {},
        "components": {"schemas": {}, "securitySchemes": {}},
    }

    for service, _rel in SERVICES:
        merged["tags"].append(
            {
                "name": service,
                "description": f"{service} 提供的端点（合并自 {service}/main.py）",
            }
        )

    for service, schema in schemas.items():
        # --- paths ---
        for path, path_item in (schema.get("paths") or {}).items():
            target_item = merged["paths"].setdefault(path, {})
            for key, value in path_item.items():
                if key in HTTP_METHODS:
                    op = dict(value)
                    _tag_operation(op, service)
                    if key in target_item:
                        # 同一 (path, method) 冲突：下游定义覆盖 gateway，
                        # tag 取并集（把先前来源服务保留下来）
                        for tag in target_item[key].get("tags") or []:
                            if tag not in op["tags"]:
                                op["tags"].append(tag)
                    target_item[key] = op
                elif key not in target_item:
                    # path 级 parameters 等
                    target_item[key] = value

        # --- components ---
        components = schema.get("components") or {}
        for section in ("schemas", "securitySchemes"):
            merged_section = merged["components"].setdefault(section, {})
            for name, definition in (components.get(section) or {}).items():
                merged_section.setdefault(name, definition)

    # 补齐缺参数的 path 模板（api-gateway 代理路由）
    for path, item in merged["paths"].items():
        _normalize_path_params(path, item)

    # operationId 去重 + 固定 key 顺序（保证可复现）
    _normalize_operation_ids(merged)
    _finalize_path_items(merged)

    # 稳定输出：path / tags 排序
    merged["paths"] = dict(sorted(merged["paths"].items()))
    merged["tags"] = sorted(merged["tags"], key=lambda t: t["name"])
    return merged


def _stats(merged: dict) -> tuple[int, int, int]:
    """返回 (端点数=唯一 path+method 对, path 数, tag 数)。"""
    endpoint_count = 0
    for path_item in merged["paths"].values():
        endpoint_count += sum(1 for k in path_item if k in HTTP_METHODS)
    return endpoint_count, len(merged["paths"]), len(merged["tags"])


def main() -> int:
    parser = argparse.ArgumentParser(description="导出并合并 Stashbox OpenAPI 3.1 schema")
    parser.add_argument("--stdout", action="store_true", help="只打印到 stdout，不落盘")
    parser.add_argument("--out", default=str(OUT_PATH), help="输出路径")
    args = parser.parse_args()

    schemas = export_service_schemas()
    for service, schema in schemas.items():
        print(f"  ✓ {service}: {len(schema.get('paths') or {})} paths")

    merged = build_merged_schema(schemas)
    endpoint_count, path_count, tag_count = _stats(merged)

    payload = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=False)
    if args.stdout:
        print(payload)
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        print(f"  ✓ 已写入 {out}")

    print(f"合并 schema：{path_count} paths / {endpoint_count} 端点 / {tag_count} tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
