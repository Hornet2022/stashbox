#!/usr/bin/env python3
"""全量端点冒烟：把客户端用到的每个端点**真的调一遍**，看哪些通、哪些断。

## 为什么需要这个

静态比对（客户端抽端点 vs 后端路由表）只能发现「调了服务端没有的」。
它发现不了「端点在、但一调就 500」「鉴权挂了」「响应结构对不上」。
那些只有**真打一遍**才看得见。

## 安全约束

- **GET 全跑**（无副作用）。
- **POST/PUT 只跑白名单里的**（幂等或纯上报），创建/删除类**不跑**
  —— 那会往生产库里造数据。创建类由手工与 E2E 用例覆盖。
- 只读生产库取真实 id，不写。

## 输出

每个端点一行：`METHOD path → HTTP 状态 业务码 备注`。
最后给出分组统计：通 / 断链 / 参数问题。

用法：
    python3 scripts/endpoint_smoke.py [--base https://api.stashbox.cn] [--admin]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ── 路径参数的真实值（从生产库取，见 fetch_params）────────────────
# 客户端调用点（来自 Retrofit 注解 + admin-web 的 apiClient 调用）
ANDROID_GET = [
    "/api/v1/users/me/tts-preference",
    "/api/v1/users/me/consent",
    "/api/v1/policies/privacy",
    "/api/v1/app/version",
    "/api/v1/articles",
    "/api/v1/articles/{article_id}",
    "/api/v1/articles/{article_id}/audio-url",
    "/api/v1/subscription/plans",
    "/api/v1/quota",
    "/api/v1/favorites/folders",
    "/api/v1/tags",
    "/api/v1/tags/{tag_id_or_slug}",
    "/api/v1/notifications",
    "/api/v1/distill/{task_id}",
    "/api/v1/distill/{task_id}/variants",
    "/api/v1/users/me/onboarding",
    "/api/v1/users/me/devices",
    "/api/v1/subscription/list",
    "/api/v1/later-listens",
    "/api/v1/articles/{article_id}/related",
]

# 幂等 / 纯上报类（有副作用但可重复、且不改用户可见状态）
ANDROID_WRITE_SAFE = [
    ("POST", "/api/v1/articles/{article_id}/progress"),
    ("POST", "/api/v1/articles/{article_id}/skip"),
    ("POST", "/api/v1/articles/{article_id}/listen-complete"),
    ("POST", "/api/v1/tags/{tag_id_or_slug}/subscribe"),
    ("POST", "/api/v1/tags/{tag_id_or_slug}/unsubscribe"),
]

ADMIN_GET = [
    "/api/v1/admin/stats",
    "/api/v1/admin/distill-p95",
    "/api/v1/admin/articles",
    "/api/v1/admin/audio-variants/stats",
    "/api/v1/admin/audit-log",
    "/api/v1/admin/consents",
    "/api/v1/admin/evaluations",
    "/api/v1/admin/evaluations/agreement",
    "/api/v1/admin/few-shot-pool/health",
    "/api/v1/admin/few-shot-pool",
    "/api/v1/admin/few-shot-pool/audit-sample",
    "/api/v1/admin/llm/config",
    "/api/v1/admin/tts/config",
    "/api/v1/admin/tts/voices",
    "/api/v1/admin/users",
    "/api/v1/admin/ab-report",
    "/api/v1/admin/push/health",
    "/api/v1/admin/push-notifications",
]


def sh(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.stdout.strip()


def fetch_params(base: str) -> dict[str, str]:
    """从生产库取真实的路径参数（只读）。"""
    out = {}
    sql = {
        "article_id": "select id from articles where deleted_at is null and status='ready' limit 1",
        "task_id": "select id from distilled_articles limit 1",
        "tag_id_or_slug": "select slug from tags limit 1",
    }
    for k, q in sql.items():
        v = sh(
            [
                "ssh",
                "-i",
                str(Path.home() / ".ssh/stashbox_deploy"),
                "-o",
                "StrictHostKeyChecking=no",
                "root@8.148.9.151",
                f'sudo -u stashbox psql stashbox -t -A -c "{q}" 2>/dev/null',
            ]
        )
        out[k] = v.splitlines()[0].strip() if v else ""
    return out


def login(base: str, code: str) -> tuple[str, str]:
    """返回 (user_token, admin_token)。"""
    r = sh(
        [
            "curl",
            "-sk",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"code": code}),
            f"{base}/api/v1/auth/wechat-login",
        ]
    )
    try:
        user_tok = json.loads(r).get("access_token", "")
    except json.JSONDecodeError:
        user_tok = ""

    r2 = sh(
        [
            "curl",
            "-sk",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"email": "admin@stashbox.local", "password": "wrong"}),
            f"{base}/api/v1/admin/auth/login",
        ]
    )
    try:
        admin_tok = json.loads(r2).get("data", {}).get("access_token", "")
    except (json.JSONDecodeError, AttributeError):
        admin_tok = ""
    return user_tok, admin_tok


def call(base: str, method: str, path: str, token: str) -> tuple[int, int, str]:
    """返回 (http_status, business_code, 备注)。"""
    args = [
        "curl",
        "-sk",
        "-o",
        "/tmp/_smoke.json",
        "-w",
        "%{http_code}",
        "-X",
        method,
        f"{base}{path}",
        "-H",
        f"Authorization: Bearer {token}",
    ]
    if method in ("POST", "PUT", "PATCH"):
        args += ["-H", "Content-Type: application/json", "-d", "{}"]
    status = sh(args)
    body = (
        Path("/tmp/_smoke.json").read_text(errors="ignore")[:120]
        if Path("/tmp/_smoke.json").exists()
        else ""
    )
    code = 0
    try:
        code = int(json.loads(body).get("code", 0))
    except (json.JSONDecodeError, AttributeError, TypeError):
        code = 0
    return int(status or 0), code, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://api.stashbox.cn")
    ap.add_argument("--admin", action="store_true", help="只跑 admin 端点")
    args = ap.parse_args()

    params = fetch_params(args.base)
    missing = [k for k, v in params.items() if not v]
    if missing:
        print(f"⚠️ 取不到路径参数：{missing} → 相关端点会带字面量占位符", file=sys.stderr)

    user_tok, admin_tok = login(args.base, "smoke_probe_audit")
    if not user_tok:
        print("✗ 拿不到用户 token，冒烟无法进行", file=sys.stderr)
        return 2
    print(f"✓ 用户 token 已获取；admin token: {'有' if admin_tok else '无（需真实密码）'}")

    results: list[tuple[str, str, int, int, str]] = []

    def fill(p: str) -> str:
        for k, v in params.items():
            p = p.replace("{" + k + "}", v or "MISSING")
        return p

    if not args.admin:
        print("\n=== 用户端 GET ===")
        for p in ANDROID_GET:
            path = fill(p)
            st, code, body = call(args.base, "GET", path, user_tok)
            results.append(("GET", path, st, code, body))
            print(f"  {st:3d} code={code:<6} {path}")

        print("\n=== 用户端 幂等 POST/PUT ===")
        for m, p in ANDROID_WRITE_SAFE:
            path = fill(p)
            st, code, body = call(args.base, m, path, user_tok)
            results.append((m, path, st, code, body))
            print(f"  {st:3d} code={code:<6} {m} {path}")

    print("\n=== 后台 GET（需要 admin token）===")
    for p in ADMIN_GET:
        path = fill(p)
        st, code, body = call(args.base, "GET", path, admin_tok)
        results.append(("GET", path, st, code, body))
        print(f"  {st:3d} code={code:<6} {path}")

    print("\n=== 汇总 ===")
    ok = [r for r in results if 200 <= r[2] < 300 and r[3] in (0, 200)]
    missing = [r for r in results if r[2] == 404]
    auth = [r for r in results if r[2] in (401, 403)]
    other = [r for r in results if r not in ok and r[2] != 404 and r[2] not in (401, 403)]
    print(f"  通          {len(ok)}/{len(results)}")
    print(f"  404 断链    {len(missing)}")
    print(f"  401/403     {len(auth)}")
    print(f"  其它异常    {len(other)}")
    for label, group in (("404", missing), ("401/403", auth), ("其它", other)):
        if group:
            print(f"\n  ── {label} ──")
            for m, p, st, code, body in group:
                print(f"    {m:6s} {p}\n           {body[:100]}")
    Path("/tmp/smoke_results.json").write_text(
        json.dumps(
            [
                {"m": m, "p": p, "http": st, "code": c, "body": b[:200]}
                for m, p, st, c, b in results
            ],
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
