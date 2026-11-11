"""变异检验：把「剪藏后台抓取」的实现**逐条改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_capture_background.py

为什么要有这个脚本：
护栏最常见的失败方式不是「写错」，而是「**写对了但没牙齿**」——
改了实现，护栏照样绿，于是下一个人以为有保护。

这次改造动的是剪藏的**核心语义**（先抓后扣 / 不重复计费 / 失败必须点名 /
孤儿必须收尾），每一条都值得钉一颗钉子。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/content/test_capture_background.py"

# (变异名, 目标文件, [(原片段, 替换片段)], 相关测试)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: 正在抓的文章被重复驱动（抓两次 ⇒ 扣两次配额）",
        "content-service/capture_pipeline.py",
        [
            (
                "    if in_flight:\n        return \"await\"",
                "    if in_flight:\n        return \"refetch\"",
            )
        ],
        [
            f"{TEST_FILE}::test_抓取中重复提交_同一篇且只扣一次",
            f"{TEST_FILE}::test_查重决策_没正文但正在抓就等着",
        ],
    ),
    (
        "M2: 失败的文章不再重新驱动抓取（用户永远剪不进去）",
        "content-service/capture_pipeline.py",
        [
            (
                "    if in_flight:\n        return \"await\"\n    return \"refetch\"",
                "    if in_flight:\n        return \"await\"\n    return \"reuse\"",
            )
        ],
        [
            f"{TEST_FILE}::test_失败的文章再提交_重新驱动抓取",
            f"{TEST_FILE}::test_查重决策_没正文也没人在抓就重新抓",
        ],
    ),
    (
        "M3: 抓取结束后不清在跑标记（文章被永久当成「正在抓」）",
        "content-service/capture_pipeline.py",
        [
            (
                "        await clear_inflight(article_id)",
                "        pass  # 变异：不清标记",
            )
        ],
        [f"{TEST_FILE}::test_失败的文章再提交_重新驱动抓取"],
    ),
    (
        "M4: 抓不到正文也扣配额（违反「先抓后扣」）",
        "content-service/capture_pipeline.py",
        [
            (
                '            art.status = "failed"\n            art.error = msg',
                '            art.status = "failed"\n            art.error = msg\n'
                "            await quota_service.consume(session, art.user_id)",
            )
        ],
        [f"{TEST_FILE}::test_后台抓不到_标记失败且不扣配额"],
    ),
    (
        "M5: 「没有正文」只用 IS NULL 判（JSON null 判不出来 ⇒ 孤儿永不收尾）",
        "content-service/capture_pipeline.py",
        [
            (
                "                        NO_RAW_CONTENT,",
                "                        Article.raw_content.is_(None),",
            )
        ],
        [f"{TEST_FILE}::test_兜底定时器_把孤儿标记为失败"],
    ),
    (
        "M6: 兜底定时器不看在跑标记（把正在抓的文章误杀）",
        "content-service/capture_pipeline.py",
        [
            (
                "            if await is_inflight(art.id):\n                continue",
                "            pass  # 变异：不看在跑标记",
            )
        ],
        [f"{TEST_FILE}::test_兜底定时器_不动正在抓的"],
    ),
    (
        "M7: 回执不再标明「后台抓取中」（客户端分不清抓取中与失败）",
        "content-service/main.py",
        [
            (
                "        reused=False,\n        fetch_pending=True,\n        quota_pending=True,",
                "        reused=False,\n        fetch_pending=False,\n        quota_pending=True,",
            )
        ],
        [f"{TEST_FILE}::test_剪藏接口立即返回_不等抓取"],
    ),
    (
        "M8: 没有正文的文章也派蒸馏（占位串会被当正文喂给 LLM）",
        "ai-service/main.py",
        [
            (
                "    if not art.raw_content:\n        raise InvalidRequest(",
                "    if False:\n        raise InvalidRequest(",
            )
        ],
        ["tests/ai/test_distill_start_idempotent.py::test_没有正文的文章不派蒸馏"],
    ),
    (
        "M9: 失败原因不再写进文章（用户只看到「失败」两个字）",
        "content-service/capture_pipeline.py",
        [
            (
                "            art.status = \"failed\"\n            art.error = msg",
                "            art.status = \"failed\"\n            art.error = None",
            )
        ],
        [f"{TEST_FILE}::test_后台抓不到_标记失败且不扣配额"],
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
