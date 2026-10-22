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
import os
import subprocess
import sys
from pathlib import Path

# ── 端点清单：从 client_endpoints.json 读（自动抽取，不是手写）────────
#
# 手写清单会骗人：2026-10-22 第一版里我凭印象写了
# `GET /api/v1/users/me/onboarding`，而安卓端真实定义的是
# `POST /api/v1/users/me/onboarding/start|step|done` —— 手写的那条
# 报 404，看起来像断链，其实**客户端根本没调过它**。
# 清单必须来自代码。

SNAPSHOT = Path(__file__).resolve().parent / "client_endpoints.json"

# 会话态端点：数据只存在**内存**里，没有 id 可从库里取。
# 没有活跃会话时 404 是**正确**行为，不该算进「断链」。
# 盲测会话存在 ai-service 的 _blind_test_sessions（24h TTL），
# 库里查不到 → 冒烟只能拿占位符去撞 → 404 是预期的。
SESSION_SCOPED = {("GET", "/api/v1/admin/tts/blind-test/{p}/results")}


def load_endpoints() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """返回 (安卓端端点, admin-web 端点)。"""
    data = json.loads(SNAPSHOT.read_text())
    and_ep = [(e["method"], e["path"]) for e in data["android"]]
    adm_ep = [(e["method"], e["path"]) for e in data["admin_web"]]
    return and_ep, adm_ep


# 有副作用、但幂等或纯上报 → 可以安全真调
SAFE_WRITE = {
    ("POST", "/api/v1/articles/{p}/progress"),
    ("POST", "/api/v1/articles/{p}/skip"),
    ("POST", "/api/v1/articles/{p}/listen-complete"),
    ("POST", "/api/v1/tags/{p}/subscribe"),
    ("POST", "/api/v1/tags/{p}/unsubscribe"),
    ("PUT", "/api/v1/users/me/consent"),
    ("PUT", "/api/v1/users/me/tts-preference"),
    ("POST", "/api/v1/users/me/onboarding/step"),
}


def sh(cmd: list[str]) -> str:
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    r = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    return r.stdout.strip()


def env_val(key: str) -> str:
    """从服务器 env 文件取值（脚本在服务器上跑时直接读得到）。"""
    fp = Path("/etc/stashbox/stashbox.env")
    if fp.exists():
        for line in fp.read_text(errors="ignore").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


def dsn() -> str:
    """PG 连接串（用 URL 连，避免 sudo 在 subprocess 里没有 tty 而失败）。"""
    return (
        f"postgresql://{env_val('POSTGRES_USER') or 'stashbox'}:"
        f"{env_val('POSTGRES_PASSWORD')}@127.0.0.1:{env_val('POSTGRES_PORT') or '5432'}/"
        f"{env_val('POSTGRES_DB') or 'stashbox'}"
    )


def fetch_params(base: str) -> dict[str, str]:
    """从生产库取真实的路径参数（只读）。"""
    out = {}
    sql = {
        "article_id": "select id from articles where deleted_at is null and status='ready' limit 1",
        "task_id": "select id from distilled_articles limit 1",
        "tag_id_or_slug": "select slug from tags limit 1",
        "favorite_id": "select id from favorites limit 1",
        "notification_id": "select id from notifications limit 1",
        "device_id": "select id from device_tokens limit 1",
    }
    for k, q in sql.items():
        v = sh(["psql", dsn(), "-t", "-A", "-c", q])
        if not v:
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


def read_admin_password(env_path: str) -> str:
    """从 env 文件里读管理员密码。

    ⚠️ **只读**，且**不打印**。
    env 文件是服务器上的 /etc/stashbox/stashbox.env（0640 root:stashbox）。
    本地没有就返回空字符串（脚本会把 admin 段跳过，而不是假装测过）。
    """
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ADMIN_BOOTSTRAP_PASSWORD="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def login(base: str, code: str, admin_password: str = "") -> tuple[str, str]:
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

    if not admin_password:
        # 没有密码就**不去试**。原实现拿写死的 "wrong" 去试 ——
        # 必然 401 → token 空 → 所有 admin 请求都是「无鉴权」→
        # 网关把它兜底成 502「暂时连不上后端服务」→
        # **看起来像 admin 路由全挂**，实际只是没登录。
        # 这个误判在 2026-10-22 真的发生过一次。
        return user_tok, ""

    r2 = sh(
        [
            "curl",
            "-sk",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"email": "admin@stashbox.local", "password": admin_password}),
            f"{base}/api/v1/admin/auth/login",
        ]
    )
    try:
        d = json.loads(r2)
        # ⚠️ admin 登录返回的是**顶层** access_token（没有 data 包装）。
        # 取错位置 → token 为空 → 所有 admin 请求都是「无鉴权」→
        # 网关把它兜底成 502「暂时连不上后端服务」→ 会被误判成「服务挂了」。
        admin_tok = d.get("access_token") or (d.get("data") or {}).get("access_token", "")
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


def fill_path(path: str, params: dict[str, str]) -> str:
    """把路径里的 {p} 按**位置语义**换成真实 id。

    同一个 {p} 在不同路径里代表不同的东西：
    /api/v1/articles/{p}/…  → article_id
    /api/v1/distill/{p}/…   → task_id（第二个是 bitrate）
    /api/v1/tags/{p}         → tag slug
    """
    rules = [
        ("/api/v1/articles/", "article_id"),
        ("/api/v1/distill/", "task_id"),
        ("/api/v1/tags/", "tag_id_or_slug"),
        ("/api/v1/favorites/", "favorite_id"),
        ("/api/v1/notifications/", "notification_id"),
        ("/api/v1/users/me/devices/", "device_id"),
    ]
    out = path
    for prefix, key in rules:
        if not out.startswith(prefix):
            continue
        out = out.replace("{p}", params.get(key) or "MISSING", 1)
        break
    # 剩下的 {p}（如 variants/{bitrate}/warm）→ 用一个常见码率
    out = out.replace("{p}", "96k")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://api.stashbox.cn")
    ap.add_argument("--admin", action="store_true", help="只跑 admin 端点")
    ap.add_argument(
        "--admin-env",
        default="/etc/stashbox/stashbox.env",
        help="含 ADMIN_BOOTSTRAP_PASSWORD 的 env 文件（只读，不打印内容）",
    )
    args = ap.parse_args()

    params = fetch_params(args.base)
    missing = [k for k, v in params.items() if not v]
    if missing:
        print(f"⚠️ 取不到路径参数：{missing} → 相关端点会带字面量占位符", file=sys.stderr)

    user_tok, admin_tok = login(args.base, "smoke_probe_audit", read_admin_password(args.admin_env))
    if not user_tok:
        print("✗ 拿不到用户 token，冒烟无法进行", file=sys.stderr)
        return 2
    print(f"✓ 用户 token 已获取；admin token: {'有' if admin_tok else '无（需真实密码）'}")

    results: list[tuple[str, str, int, int, str]] = []

    and_ep, adm_ep = load_endpoints()

    if not args.admin:
        print("\n=== 安卓端 GET（全部）===")
        for m, p in and_ep:
            if m != "GET":
                continue
            path = fill_path(p, params)
            st, code, body = call(args.base, m, path, user_tok)
            results.append((m, path, st, code, body))
            print(f"  {st:3d} code={code:<6} {m:6s} {path}")

        print("\n=== 安卓端 幂等 POST/PUT ===")
        for m, p in and_ep:
            if (m, p) not in SAFE_WRITE:
                continue
            path = fill_path(p, params)
            st, code, body = call(args.base, m, path, user_tok)
            results.append((m, path, st, code, body))
            print(f"  {st:3d} code={code:<6} {m:6s} {path}")

    print("\n=== admin-web GET（需要 admin token）===")
    if not admin_tok:
        # ⚠️ 这里原来只 print 一句「跳过」，**却没真的跳过** ——
        # 循环照样跑，于是所有 admin 端点都报 502「连不上后端服务」，
        # 而真实原因只是没登录。工具自己制造了一个假断链。
        print("  ⚠️ 没拿到 admin token → **真的跳过**（拿不到就是测不准，不是端点坏了）")
        print("     提示：--admin-env 指向含 ADMIN_BOOTSTRAP_PASSWORD 的 env 文件")
    else:
        for m, p in adm_ep:
            if m != "GET":
                continue
            path = fill_path(p, params)
            st, code, body = call(args.base, m, path, admin_tok)
            results.append((m, path, st, code, body))
            print(f"  {st:3d} code={code:<6} {m:6s} {path}")

    print("\n=== 汇总 ===")

    # 分类要**诚实**：把「端点坏了」和「脚本没条件」分开。
    #
    # 之前只有三档（通 / 404 断链 / 其它），于是两类假警报混在里面：
    #   · 盲测结果端点：盲测会话只存在**内存**里（_blind_test_sessions，24h TTL），
    #     库里没有 id 可取 → 冒烟只能拿占位符去撞 → 404 是**预期**行为；
    #   · 属主校验：冒烟用 probe 用户登录，却拿库里**别人的**真实 id 去调
    #     → 403「not the owner」是**校验生效**，不是断链。
    # 这两类混进「404 断链」，会让人以为服务坏了。
    def _is_session_scoped(m: str, p: str) -> bool:
        """p 是**填完参数**的路径，SESSION_SCOPED 里是**模板**。

        所以不能直接比字符串 —— 得把模板拆成「前缀 / 后缀」，
        中间那段（填进去的参数）不参与比较。
        """
        for tm, tpl in SESSION_SCOPED:
            if tm != m:
                continue
            head, _, tail = tpl.partition("{")
            tail = tail.rpartition("}")[2]
            if p.startswith(head) and p.endswith(tail) and len(p) >= len(head) + len(tail):
                return True
        return False

    def _is_owner_guard(body: str) -> bool:
        return "not the owner" in body

    ok = [r for r in results if 200 <= r[2] < 300 and r[3] in (0, 200)]
    session_absent = [r for r in results if r[2] == 404 and _is_session_scoped(r[0], r[1])]
    missing = [r for r in results if r[2] == 404 and not _is_session_scoped(r[0], r[1])]
    owner_guard = [r for r in results if r[2] == 403 and _is_owner_guard(r[4])]
    auth_fail = [
        r
        for r in results
        if r[2] in (401, 403)
        and r not in owner_guard
        and r not in missing
        and r not in session_absent
    ]
    param = [
        r
        for r in results
        if r[2] in (400, 422) and r not in ok and r not in missing and r not in session_absent
    ]
    other = [
        r
        for r in results
        if r not in ok
        and r not in missing
        and r not in session_absent
        and r not in owner_guard
        and r not in auth_fail
        and r not in param
    ]
    print(f"  通            {len(ok)}/{len(results)}")
    print(f"  真断链(404)   {len(missing)}")
    print(f"  属主校验(403) {len(owner_guard)}  ← 校验生效，不是断链")
    print(f"  鉴权失败      {len(auth_fail)}")
    print(f"  参数不足      {len(param)}  ← 脚本没填 body/query，不是断链")
    print(f"  会话不存在    {len(session_absent)}  ← 会话态端点，404 是预期")
    print(f"  其它异常      {len(other)}")
    for label, group in (
        ("真断链", missing),
        ("鉴权失败", auth_fail),
        ("参数不足", param),
        ("其它", other),
    ):
        if group:
            print(f"\n  ── {label} ──")
            for m, p, st, code, body in group:
                print(f"    {m:6s} {p}\n           {body[:100]}")
    if not missing and not auth_fail and not other:
        print("\n  ✅ 没有真断链")
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
