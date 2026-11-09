"""变异检验：把「三处真相源」判据**逐条改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_release_consistency.py

为什么要有这个脚本：
护栏最常见的失败方式不是"写错"，而是"**写对了但没牙齿**" ——
改了实现，护栏照样绿，于是下一个人以为有保护。
所以每条判据都要**亲手把它守护的东西破坏一次**，确认它真的会红。

这个护栏守的是"安卓内测发版后「当前版本」三处是否一致"：
2026-11-09 内测反馈「无法下载新版本」，根因就是手工发版漏了 env 那一步
⇒ 包在、软链对、但版本接口说旧号 ⇒ 客户端判"已是最新版本" ⇒ 根本没有下载入口。

每条判据单独破坏一次 ⇒ 对应用例必须变红 ⇒ 护栏有牙齿。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/infra/test_release_consistency.py"

# (变异名, 目标文件, [(原片段, 替换片段)], 相关测试)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: 抽掉判据1（env↔接口 versionCode）",
        "deploy/release_consistency.py",
        [("    if str(api_code) != env_code:", "    if False:")],
        [f"{TEST_FILE}::test_判据1_env与接口的versionCode不一致"],
    ),
    (
        "M2: 抽掉判据2（env↔接口 版本名）",
        "deploy/release_consistency.py",
        [("    if api_name != env_name:", "    if False:")],
        [f"{TEST_FILE}::test_判据2_env与接口的版本名不一致"],
    ),
    (
        "M3: 抽掉判据3（env↔接口 下载地址）",
        "deploy/release_consistency.py",
        [("    if api_url != env_url:", "    if False:")],
        [f"{TEST_FILE}::test_判据3_env与接口的下载地址不一致"],
    ),
    (
        "M4: 抽掉判据4（接口下发的包≠软链指向的包）",
        "deploy/release_consistency.py",
        [("        if want_name != link:", "        if False:")],
        [f"{TEST_FILE}::test_判据4_接口下发的包与软链指向不一致"],
    ),
    (
        "M5: 抽掉判据5（文件不在磁盘上）",
        "deploy/release_consistency.py",
        [("        if want_name and not file_exists:", "        if False:")],
        [f"{TEST_FILE}::test_判据5_软链指向的文件不在磁盘上"],
    ),
    (
        "M6: 抽掉判据6（软链版本≠env 版本名 —— 2026-11-09 踩的坑）",
        "deploy/release_consistency.py",
        [('    if m and env_name and m.group("version") != env_name:', "    if False:")],
        [f"{TEST_FILE}::test_判据6_软链版本与env版本名不一致"],
    ),
    (
        "M7: 抽掉判据7（versionCode 必须正整数）",
        "deploy/release_consistency.py",
        [("    if not env_code.isdigit() or int(env_code) <= 0:", "    if False:")],
        [
            f"{TEST_FILE}::test_判据7_versionCode为零",
            f"{TEST_FILE}::test_判据7_versionCode不是数字",
        ],
    ),
    (
        "M8: 抽掉判据8（更新说明非空）",
        "deploy/release_consistency.py",
        [("    if not env_notes.strip():", "    if False:")],
        [f"{TEST_FILE}::test_判据8_更新说明为空"],
    ),
    (
        "M9: 抽掉判据9（发布时间非空）",
        "deploy/release_consistency.py",
        [("    if not env_published.strip():", "    if False:")],
        [f"{TEST_FILE}::test_判据9_发布时间为空"],
    ),
    (
        "M10: 抽掉判据10（env↔接口 发布时间）",
        "deploy/release_consistency.py",
        [
            (
                # 锚点用**单行内的短片段**：ruff format 会把长条件拆成多行，
                # 用整条多行条件当锚点 ⇒ 格式化一次锚点就失配 ⇒ 变异检验假失败。
                "and env_published.strip() != api_published.strip()",
                "and False",
            )
        ],
        [f"{TEST_FILE}::test_判据10_env与接口的发布时间不一致"],
    ),
    (
        "M11: 抽掉判据11（env↔接口 更新说明 —— 2026-10-25 踩的坑）",
        "deploy/release_consistency.py",
        [
            (
                "and env_notes.strip() != api_notes.strip()",
                "and False",
            )
        ],
        [f"{TEST_FILE}::test_判据11_env与接口的更新说明不一致"],
    ),
    (
        "M12: env 解析不剥引号（systemd 语义丢失 ⇒ 巡检永远报不一致）",
        "deploy/release_consistency.py",
        [("        out[key] = _unquote(value.strip())", "        out[key] = value.strip()")],
        [f"{TEST_FILE}::test_parse_env_block_剥掉成对引号"],
    ),
    (
        "M13: env 解析按最后一个等号切分（值里有等号就错）",
        "deploy/release_consistency.py",
        [
            (
                '        key, _, value = line.partition("=")',
                '        key, _, value = line.rpartition("=")',
            )
        ],
        [f"{TEST_FILE}::test_parse_env_block_值里有等号按第一个等号切分"],
    ),
    (
        "M14: 巡检脚本把判定内嵌回 shell（判据变成两份实现 ⇒ 没牙齿）",
        "deploy/verify_release_consistency.sh",
        [
            (
                'python3 "$(dirname "$0")/release_consistency.py"',
                "python3 - <<'PY'\nprint('ok')\nPY",
            )
        ],
        [f"{TEST_FILE}::test_判据只有一份实现_巡检脚本不内嵌判定逻辑"],
    ),
    (
        "M15: 发版脚本不再复用共享巡检（判据分叉）",
        "deploy/release_apk.sh",
        [
            (
                'bash "$(dirname "$0")/verify_release_consistency.sh" || {',
                "true || {",
            )
        ],
        [f"{TEST_FILE}::test_判据只有一份实现_发版脚本复用同一个巡检"],
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
                print(f"❌ {name}\n   锚点没命中：{rel_path} 里找不到 {old[:60]!r}")
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
