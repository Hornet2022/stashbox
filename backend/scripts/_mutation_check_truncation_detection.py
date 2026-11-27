"""变异检验：把 2026-11-27 截断检测修复的每条护栏对应的实现**改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_truncation_detection.py

为什么要有这个脚本：
护栏最常见的失败方式不是"写错"，而是"**写对了但没牙齿**" ——
改了实现，护栏照样绿，于是下一个人以为有保护。
所以每条护栏都要**亲手把它守护的东西破坏一次**，确认它真的会红。

本脚本覆盖 4 个变异（每个都对应生产事故的一环）：
  M1  阈值放回 11.0        ⇒ 真截断（8.14 / 9.44）从阈值下面溜过去（**这正是事故现场**）
  M2  严格档门槛归零        ⇒ 短块被严格档误判（admin 的 TTS 测试按钮会红）
  M3  关掉同文本重渲        ⇒ 自愈只能切小（接缝变多、命中变低）
  M4  克隆路径也同文本重渲  ⇒ 重采样说话人 ⇒ 一篇里出现两个人

任一步不符合预期 ⇒ 退出码非 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: 截断阈值放回 11.0（真截断 8.14/9.44 从阈值下面溜过去 = 事故现场）",
        "app/services/tts/openai.py",
        [
            (
                "_TRUNCATION_CPS_PRESET_LONG = 6.0",
                "_TRUNCATION_CPS_PRESET_LONG = 11.0",
            )
        ],
        [
            "tests/tts/test_tts_truncation_detection.py::test_生产实测截断块必须被抓出来",
            "tests/tts/test_tts_truncation_detection.py::test_阈值分档_预设长块用严格档",
        ],
    ),
    (
        "M2: 严格档门槛归零（短块也被严格档判 ⇒ admin TTS 测试按钮变红）",
        "app/services/tts/openai.py",
        [
            (
                "_TRUNCATION_STRICT_MIN_CHARS = 150",
                "_TRUNCATION_STRICT_MIN_CHARS = 0",
            )
        ],
        [
            "tests/tts/test_tts_truncation_detection.py::test_阈值分档_预设长块用严格档",
        ],
    ),
    (
        "M3: 关掉同文本重渲（自愈只能切小 ⇒ 接缝变多、命中变低）",
        "app/services/tts/openai.py",
        [
            (
                "_MAX_SAME_TEXT_RETRIES = 1",
                "_MAX_SAME_TEXT_RETRIES = 0",
            )
        ],
        [
            "tests/tts/test_tts_truncation_detection.py::test_检出截断后先同文本重渲",
        ],
    ),
    (
        "M4: 克隆路径也做同文本重渲（重采样说话人 ⇒ 一篇里出现两个人）",
        "app/services/tts/openai.py",
        [
            (
                "            if clone is None and depth < _MAX_SPLIT_DEPTH:",
                "            if depth < _MAX_SPLIT_DEPTH:",
            )
        ],
        [
            "tests/tts/test_tts_truncation_detection.py::test_克隆路径不做同文本重渲",
        ],
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
            print(out[-1500:])
            continue

        code2, out2 = _run(tests)
        if code2 != 0:
            failures.append(name)
            print(f"❌ {name}\n   还原后仍然红 —— 护栏本身有问题")
            print(out2[-2000:])
            continue

        print(f"✅ {name}\n   变异变红 ✅ 还原变绿 ✅")

    print()
    if failures:
        print(f"❌ {len(failures)}/{len(MUTATIONS)} 个变异未被捕获：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {len(MUTATIONS)}/{len(MUTATIONS)} 个变异均被捕获（护栏有牙齿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
