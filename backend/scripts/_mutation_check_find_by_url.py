"""变异检验：把「按 URL 幂等回查」的实现**逐条改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_find_by_url.py

为什么要有这个脚本：
护栏最常见的失败方式不是"写错"，而是"**写对了但没牙齿**" ——
改了实现，护栏照样绿，于是下一个人以为有保护。

这个护栏守的是 2026-11-11 内测实测到的故障：
剪藏是同步长请求 ⇒ 客户端侧抖动会变成「**服务端成功、客户端报错**」
⇒ 客户端失败时必须能回查"你是不是其实收到了"。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/content/test_find_article_by_url.py"

# (变异名, 目标文件, [(原片段, 替换片段)], 相关测试)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: 回查不再按用户过滤（越权能看到别人的剪藏）",
        "content-service/main.py",
        [
            (
                "    dup = await _find_existing_clip(url, _uid(user), db)",
                "    dup = await db.scalar(\n"
                "        select(Article)\n"
                "        .where(Article.url == url, Article.deleted_at.is_(None))\n"
                "        .order_by(Article.created_at.desc())\n"
                "        .limit(1)\n"
                "    )",
            )
        ],
        [f"{TEST_FILE}::test_别人的文章查不到"],
    ),
    (
        "M2: 回查恒说不存在（客户端永远查不到 ⇒ 修复形同虚设）",
        "content-service/main.py",
        [
            (
                '    return {"exists": True, "article_id": dup.id, "status": dup.status}',
                '    return {"exists": False, "article_id": None, "status": None}',
            )
        ],
        [f"{TEST_FILE}::test_剪藏过就能查到"],
    ),
    (
        "M3: 回查恒说存在（任何链接都报成功 ⇒ 客户端会说假话）",
        "content-service/main.py",
        [
            (
                "    dup = await _find_existing_clip(url, _uid(user), db)\n"
                "    if dup is None:\n"
                '        return {"exists": False, "article_id": None, "status": None}\n'
                '    return {"exists": True, "article_id": dup.id, "status": dup.status}',
                "    dup = await _find_existing_clip(url, _uid(user), db)\n"
                '    return {"exists": True, "article_id": dup.id if dup else "art_x", "status": "pending"}',
            )
        ],
        [f"{TEST_FILE}::test_查不到时返回exists_false"],
    ),
    (
        "M4: 端点声明在 {article_id} 之后（by-url 会被当成 article_id）",
        "content-service/main.py",
        [
            (
                '@app.get("/api/v1/articles/by-url")',
                '@app.get("/api/v1/articles/by-url-late")',
            )
        ],
        [
            f"{TEST_FILE}::test_按声明顺序by_url不会被当成article_id",
            f"{TEST_FILE}::test_剪藏过就能查到",
        ],
    ),
    (
        "M5: 网关路由表漏了这个端点（走兜底 ⇒ 缺请求 ID，排查串不上号）",
        "api-gateway/config.py",
        [
            (
                '    Route("GET", "/api/v1/articles/by-url", "content-service", _url("content-service")),',
                "",
            )
        ],
        [f"{TEST_FILE}::test_网关路由表必须显式注册这个端点"],
    ),
]


def _run(pytest_args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args, "-q", "--no-header", "-p", "no:randomly"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures: list[str] = []
    for name, rel_path, edits, tests in MUTATIONS:
        target = BACKEND / rel_path
        original = target.read_text(encoding="utf-8")
        mutated = original
        ok = True
        for old, new in edits:
            if old not in mutated:
                print(f"❌ {name}\n   锚点没命中：{rel_path} 里找不到 {old[:70]!r}")
                ok = False
                break
            mutated = mutated.replace(old, new, 1)
        if not ok:
            failures.append(name)
            continue

        try:
            target.write_text(mutated, encoding="utf-8")
            code, out = _run(tests)
        finally:
            target.write_text(original, encoding="utf-8")

        if code == 0:
            failures.append(name)
            print(f"❌ {name}\n   护栏**没红** —— 变异没被抓到（护栏没牙齿）")
            continue

        print(f"✓ {name} → 用例变红 ✓")

    print()
    if failures:
        print(f"❌ 变异检验失败：{len(failures)}/{len(MUTATIONS)} 条变异没被抓到")
        for f in failures:
            print(f"   · {f}")
        return 1
    print(f"✅ 变异检验通过：{len(MUTATIONS)}/{len(MUTATIONS)} 条变异全部被抓到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
