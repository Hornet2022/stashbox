#!/usr/bin/env python3
"""CP6.6：对比两个 git ref 的 OpenAPI schema，检出 breaking change。

用途（下个任务接 CI 时直接调用）：把当前分支的 schema 跟 ``origin/main``
比，出现 breaking change 就 exit 1，让 CI 失败。

判定为 **breaking** 的变更：
  - 端点被删除（path 或 method 少了）
  - 某 components schema 新增了 required 字段（必填字段加 → 老客户端会 422）
  - 某端点的 query/path/header 参数由可选变必填

非 breaking（仅提示，不影响退出码）：新增端点、新增可选字段、必填字段变可选。

用法::

    python backend/scripts/check_openapi_diff.py                      # HEAD vs origin/main
    python backend/scripts/check_openapi_diff.py --base origin/main --head HEAD
    python backend/scripts/check_openapi_diff.py --base HEAD          # 跟当前 commit 的父比较用 --head 指定
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCHEMA_REL = "backend/docs/openapi/stashbox-openapi.json"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def git_show_json(ref: str, rel: str) -> dict | None:
    """取 ``<ref>:<rel>`` 的 JSON；ref 或文件不存在返回 None。"""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def flatten_endpoints(schema: dict) -> set[tuple[str, str]]:
    """{(path, METHOD)}。"""
    endpoints: set[tuple[str, str]] = set()
    for path, item in (schema.get("paths") or {}).items():
        for key in item:
            if key in HTTP_METHODS:
                endpoints.add((path, key.upper()))
    return endpoints


def required_schema_props(schema: dict) -> set[tuple[str, str]]:
    """{(schema_name, prop)} —— components.schemas 里声明为 required 的字段。"""
    out: set[tuple[str, str]] = set()
    schemas = (schema.get("components") or {}).get("schemas") or {}
    for name, definition in schemas.items():
        for prop in definition.get("required") or []:
            out.add((name, prop))
    return out


def required_parameters(schema: dict) -> set[tuple[str, str, str]]:
    """{(path, METHOD, param_name)} —— 声明为 required 的参数。"""
    out: set[tuple[str, str, str]] = set()
    for path, item in (schema.get("paths") or {}).items():
        for key, op in item.items():
            if key not in HTTP_METHODS or not isinstance(op, dict):
                continue
            for param in op.get("parameters") or []:
                if param.get("required"):
                    out.add((path, key.upper(), str(param.get("name"))))
    return out


def diff(base: dict, head: dict) -> dict:
    base_ep = flatten_endpoints(base)
    head_ep = flatten_endpoints(head)
    base_req = required_schema_props(base)
    head_req = required_schema_props(head)
    base_par = required_parameters(base)
    head_par = required_parameters(head)
    return {
        "removed_endpoints": sorted(base_ep - head_ep),
        "added_endpoints": sorted(head_ep - base_ep),
        "new_required_props": sorted(head_req - base_req),
        "removed_required_props": sorted(base_req - head_req),
        "new_required_params": sorted(head_par - base_par),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAPI schema breaking-change 检查")
    parser.add_argument("--base", default="origin/main", help="基线 ref（默认 origin/main）")
    parser.add_argument("--head", default="HEAD", help="目标 ref（默认 HEAD）")
    args = parser.parse_args()

    base = git_show_json(args.base, SCHEMA_REL)
    if base is None:
        print(f"⚠ 基线 {args.base}:{SCHEMA_REL} 不存在（首次引入 schema？）—— 跳过检查")
        return 0

    head = git_show_json(args.head, SCHEMA_REL)
    if head is None:
        # HEAD 里没有（比如文件还没提交）：退而读工作区文件
        worktree = Path(__file__).resolve().parents[2] / SCHEMA_REL
        if worktree.is_file():
            head = json.loads(worktree.read_text(encoding="utf-8"))
            print(f"ℹ {args.head}:{SCHEMA_REL} 不存在，改用工作区文件 {worktree}")
        else:
            print(f"✗ 无法读取 {args.head}:{SCHEMA_REL}，且工作区也没有该文件", file=sys.stderr)
            return 2

    result = diff(base, head)
    breaking = (
        result["removed_endpoints"] or result["new_required_props"] or result["new_required_params"]
    )

    print(f"OpenAPI schema diff：{args.base} → {args.head}")
    for label, key in (
        ("删除端点（BREAKING）", "removed_endpoints"),
        ("新增端点", "added_endpoints"),
        ("新增必填字段（BREAKING）", "new_required_props"),
        ("必填字段改可选", "removed_required_props"),
        ("参数由可选变必填（BREAKING）", "new_required_params"),
    ):
        items = result[key]
        if not items:
            continue
        print(f"  {label}：{len(items)}")
        for item in items:
            print(f"    - {item}")

    if not any(result.values()):
        print("  ✓ 无变更")
    if breaking:
        print("\n✗ 检测到 breaking change")
        return 1
    print("\n✓ 无 breaking change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
