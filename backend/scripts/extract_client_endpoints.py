#!/usr/bin/env python3
"""抽取客户端**实际调用**的端点清单 → scripts/client_endpoints.json

## 为什么要有这个

「客户端调了但服务端没有的端点」是最好查的一类断链，但它只能靠
**机器比对**发现 —— 手写清单一定会错。

2026-10-22 实测踩到的：第一版冒烟脚本里凭印象写了
`GET /api/v1/users/me/onboarding`，而安卓端真实定义的是
`POST /api/v1/users/me/onboarding/start|step|done` —— 手写的那条报 404，
看起来像断链，**其实客户端根本没调过它**。

## 抽取来源

- 安卓：Retrofit 注解 `@METHOD("path")`（`app/src/main/kotlin/**/data/remote/*.kt`）
- admin-web：`apiClient.METHOD('path')`（`src/api/**`）

路径参数统一归一为 `{p}`（同一路径里不同位置的 `{p}` 代表不同的 id，
由 `endpoint_smoke.py` 按路径前缀分别填 article_id / task_id / tag …）。

## 用法

    python3 scripts/extract_client_endpoints.py            # 写文件
    python3 scripts/extract_client_endpoints.py --check    # 只看变化，不写

改了客户端的 API 定义后跑一次，把快照一起提交 —— 这样「客户端调用的
端点集合」就在版本控制里，谁改了、什么时候改的都看得见。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
OUT = BACKEND_DIR / "scripts/client_endpoints.json"


def _find_clients() -> tuple[Path, Path]:
    """向上找客户端仓库。

    三个仓是**同级**的（stashbox / stashbox-android / stashbox-admin-web），
    而 backend 在 stashbox 仓内 —— 所以不能只往上找一层。
    """
    for base in [BACKEND_DIR, *BACKEND_DIR.parents][:4]:
        android = base / "stashbox-android"
        admin = base / "stashbox-admin-web"
        if android.is_dir() and admin.is_dir():
            return android, admin
    raise SystemExit(
        "✗ 找不到客户端仓库（stashbox-android / stashbox-admin-web）—— "
        f"从 {BACKEND_DIR} 往上找过了"
    )


ANDROID_DIR, ADMIN_WEB_DIR = _find_clients()

RETROFIT = re.compile(r'@(GET|POST|PUT|DELETE|PATCH)\("([^"]+)"\)')
AXIOS = re.compile(r"""apiClient\.(get|post|put|delete|patch)\(\s*[`'"]([^`'"]+)""")


def norm(method: str, path: str) -> tuple[str, str]:
    p = path.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{p}", p)
    p = re.sub(r"\{[^}]+\}", "{p}", p)
    if not p.startswith("/"):
        p = "/" + p
    return (method.upper(), p)


def scan_android() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    root = ANDROID_DIR / "app/src/main/kotlin"
    # ⚠️ 必须 **sorted**：rglob 的遍历顺序取决于文件系统目录项的顺序（不是字典序），
    # 而下面用 setdefault（先到先得）决定「这个端点记在哪个文件名下」。
    # 不排序的话，同一个端点的 src 会在两次生成之间来回跳 —— 实测
    # `/api/v1/admin/stats` 有一次从 `api/admin/dashboard.ts` 变成了
    # `api/client.interceptor.test.ts`，产生了一处**与代码无关**的快照 diff，
    # 让人以为契约漂移了。归属必须可复现。
    for f in sorted(root.rglob("*.kt")):
        text = f.read_text(errors="ignore")
        for m in RETROFIT.finditer(text):
            key = norm(m.group(1), m.group(2))
            out.setdefault(key, str(f.relative_to(ANDROID_DIR)))
    return out


def scan_admin_web() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    root = ADMIN_WEB_DIR / "src"
    # 同 scan_android：排序，保证「端点 → 源文件」的归属可复现。
    for f in sorted(root.rglob("*.ts")):
        text = f.read_text(errors="ignore")
        for m in AXIOS.finditer(text):
            key = norm(m.group(1), m.group(2))
            out.setdefault(key, str(f.relative_to(ADMIN_WEB_DIR)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    args = ap.parse_args()

    if not ANDROID_DIR.exists() or not ADMIN_WEB_DIR.exists():
        print("✗ 找不到客户端仓库（应在仓库同级目录）", file=sys.stderr)
        return 2

    and_map = scan_android()
    adm_map = scan_admin_web()

    snap = {
        "说明": "客户端实际调用的端点清单（自动抽取）。冒烟脚本读它；也可用于 CI 检查契约漂移。",
        "生成方式": "Retrofit 注解 @METHOD(\"path\") + admin-web 的 apiClient.METHOD('path')；路径参数统一归一为 {p}",
        "android": [{"method": m, "path": p, "src": and_map[(m, p)]} for m, p in sorted(and_map)],
        "admin_web": [{"method": m, "path": p, "src": adm_map[(m, p)]} for m, p in sorted(adm_map)],
    }
    text = json.dumps(snap, ensure_ascii=False, indent=1)

    if args.check:
        if not OUT.exists():
            print(f"✗ {OUT} 不存在", file=sys.stderr)
            return 1
        old = json.loads(OUT.read_text())
        old_a = {(e["method"], e["path"]) for e in old.get("android", [])}
        new_a = {(e["method"], e["path"]) for e in snap["android"]}
        old_b = {(e["method"], e["path"]) for e in old.get("admin_web", [])}
        new_b = {(e["method"], e["path"]) for e in snap["admin_web"]}
        print(f"  安卓端   新增 {len(new_a - old_a)}  移除 {len(old_a - new_a)}")
        for e in sorted(new_a - old_a):
            print(f"    + {e[0]} {e[1]}")
        for e in sorted(old_a - new_a):
            print(f"    - {e[0]} {e[1]}")
        print(f"  admin-web 新增 {len(new_b - old_b)}  移除 {len(old_b - new_b)}")
        for e in sorted(new_b - old_b):
            print(f"    + {e[0]} {e[1]}")
        for e in sorted(old_b - new_b):
            print(f"    - {e[0]} {e[1]}")
        return 0

    OUT.write_text(text)
    print(f"✓ 已写入 {OUT}：android {len(and_map)} 个 / admin_web {len(adm_map)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
