"""变异检验：把「过期音频 key 重定向」的实现**逐条改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_audio_stale_key.py

为什么要有这个脚本：
护栏最常见的失败方式不是"写错"，而是"**写对了但没牙齿**" ——
改了实现，护栏照样绿，于是下一个人以为有保护。
所以每条判据都要**亲手把它守护的东西破坏一次**，确认它真的会红。

这个护栏守的是 2026-11-09 内测实测到的故障：
音频重新生成 ⇒ key 变 ⇒ 旧文件被删 ⇒ 客户端缓存的旧 URL 变成 404
⇒ 客户端把它当成「网络不稳」⇒ 界面显示「正在重连…」并反复重试。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/gateway/test_audio_stale_key_redirect.py"

# (变异名, 目标文件, [(原片段, 替换片段)], 相关测试)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: 解析器不再重定向（过期 key 退回 404）",
        "app/services/audio/stale_key.py",
        [
            (
                "    if not current_key:\n        return False\n    return current_key != requested_key",
                "    return False",
            )
        ],
        [
            f"{TEST_FILE}::test_解析器_过期key解析到当前音频的公开路径",
            f"{TEST_FILE}::test_文件缺失且key过期时返回302并指向当前版本",
        ],
    ),
    (
        "M2: 重定向状态码改成 200（客户端拿不到新地址）",
        "app/services/audio/static_files.py",
        [
            (
                "return RedirectResponse(target, status_code=302)",
                "return RedirectResponse(target, status_code=200)",
            )
        ],
        [f"{TEST_FILE}::test_文件缺失且key过期时返回302并指向当前版本"],
    ),
    (
        "M3: 重定向指回请求的旧 key（重定向到自己 ⇒ 死循环）",
        "app/services/audio/static_files.py",
        [
            (
                '            return f"{mount_prefix}/{current}"',
                '            return f"{mount_prefix}/{parsed.key}"',
            )
        ],
        [f"{TEST_FILE}::test_解析器_过期key解析到当前音频的公开路径"],
    ),
    (
        "M4: 查库失败不再兜底（静态目录被 DB 拖成 500）",
        "app/services/audio/static_files.py",
        [
            (
                '        except Exception as exc:  # noqa: BLE001 —— 兜底：解析失败按"没有"处理',
                "        except ValueError as exc:",
            )
        ],
        [f"{TEST_FILE}::test_解析器_查库失败按没有处理而不是抛异常"],
    ),
    (
        "M5: 网关不再挂带重定向的静态目录（护栏形同虚设）",
        "api-gateway/main.py",
        [
            (
                "            _AudioFilesWithStaleRedirect(\n                directory=_audio_dir,\n                check_dir=False,\n                stale_resolver=_stale_audio_resolver,\n            ),",
                "            StaticFiles(directory=_audio_dir, check_dir=False),",
            )
        ],
        [f"{TEST_FILE}::test_接线护栏_网关挂载用的是带重定向的静态目录类"],
    ),
    (
        "M6: 解析器不再注入（类退化成普通静态目录）",
        "api-gateway/main.py",
        [("                stale_resolver=_stale_audio_resolver,\n", "")],
        [f"{TEST_FILE}::test_接线护栏_网关挂载用的是带重定向的静态目录类"],
    ),
    (
        "M7: 当前音频 key 不再兜底查主档（老数据拿不到 key ⇒ 404）",
        "app/services/audio/resolve.py",
        [
            (
                "    stmt2 = select(Article.audio_url).where(Article.id == article_id).limit(1)\n"
                "    url2 = (await session.execute(stmt2)).scalar_one_or_none()\n"
                "    return key_from_audio_url(url2)",
                "    return None",
            )
        ],
        [f"{TEST_FILE}::test_当前音频key_蒸馏行没有时兜底主档"],
    ),
    (
        "M8: key 解析放宽（把非音频文件也当成音频去查库）",
        "app/services/audio/stale_key.py",
        [
            (
                'r"^audio/(?P<article_id>[A-Za-z0-9_-]+)\\.(?P<hash>[0-9a-f]{4,64})\\.(?P<ext>[A-Za-z0-9]+)$"',
                'r"^audio/(?P<article_id>[A-Za-z0-9_-]+)\\.(?P<hash>[^.]*)\\.(?P<ext>[A-Za-z0-9]+)$"',
            )
        ],
        [f"{TEST_FILE}::test_parse_audio_key_不是音频key就返回None"],
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
