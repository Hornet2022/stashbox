"""变异检验：把每条新护栏对应的实现**改坏**，确认护栏会变红。

用法（在 backend/ 下）：python scripts/_mutation_check_upstream.py

为什么要有这个脚本：
护栏最常见的失败方式不是"写错"，而是"**写对了但没牙齿**" ——
改了实现，护栏照样绿，于是下一个人以为有保护。
所以每条护栏都要**亲手把它守护的东西破坏一次**，确认它真的会红。

本脚本做的事：
  1. 备份原文件
  2. 施加一个变异（改坏实现）
  3. 跑相关用例 ⇒ 必须**红**
  4. 还原 ⇒ 必须**绿**

任一步不符合预期 ⇒ 退出码非 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# (变异名, 目标文件, [(原片段, 替换片段)], 相关测试)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str]]] = [
    (
        "M1: source 判据改回 truthiness（admin 说 defaults / 运行期说 db）",
        "common/upstream_config.py",
        [
            (
                '    return "db" if stored is not None else "defaults"',
                '    return "db" if stored else "defaults"',
            )
        ],
        ["tests/common/test_upstream_config.py::test_source判据全仓唯一"],
    ),
    (
        "M2: 行不存在时静默用代码默认值（回到'症状是 401'的老病）",
        "common/upstream_config.py",
        [
            (
                '    if strict and source == "defaults":',
                "    if False:",
            )
        ],
        ["tests/common/test_upstream_config.py::test_行不存在时运行期点名不静默用默认值"],
    ),
    (
        "M3: 必填字段缺失不点名（拿残缺配置去试）",
        "common/upstream_config.py",
        [
            (
                "        missing = missing_required(key, cfg)",
                "        missing = []",
            )
        ],
        ["tests/common/test_upstream_config.py::test_配置不完整时点名缺哪个字段"],
    ),
    (
        "M4: 行不存在也记 last-good（默认值污染兜底）",
        "common/upstream_config.py",
        [
            (
                '    if source == "db":\n        _remember(key, cfg)',
                "    _remember(key, cfg)",
            )
        ],
        ["tests/common/test_upstream_config.py::test_行不存在不污染last_good"],
    ),
    (
        "M5: admin 端点内联判 source（判据又变成两份）",
        "content-service/admin_router.py",
        [
            (
                "    return _masked_llm_config(config, source_of(stored), system_config.as_iso(updated_at))",
                "    return _masked_llm_config(\n"
                '        config, "db" if stored else "defaults", system_config.as_iso(updated_at)\n'
                "    )",
            )
        ],
        ["tests/common/test_upstream_config.py::test_admin端点用同一个source判据"],
    ),
    (
        "M6: app/services/llm 不传 DB 预算（后台改 timeout 只对一半链路生效）",
        "app/services/llm/__init__.py",
        [
            (
                '            timeout=timeout,\n            max_retries=max_retries,\n            connect_timeout=connect_timeout,\n        )\n    if provider == "qwen_vl":',
                '        )\n    if provider == "qwen_vl":',
            )
        ],
        ["tests/common/test_upstream_config.py::test_app_services_llm透传预算字段"],
    ),
    (
        "M7: LLM client 退回单一 timeout（连接超时并回读超时）",
        "app/services/llm/openai.py",
        [
            (
                "            timeout=httpx.Timeout(\n                timeout,\n                connect=min(connect_timeout, timeout),\n                read=timeout,\n                write=timeout,\n                pool=timeout,\n            ),",
                "            timeout=timeout,",
            )
        ],
        [
            "tests/common/test_upstream_config.py::test_app_services_llm透传预算字段",
            "tests/common/test_upstream_config.py::test_所有上游client都有独立连接超时",
        ],
    ),
    (
        "M8: TTS openai client 退回单一 timeout（上游黑洞白等 300s）",
        "app/services/tts/openai.py",
        [
            (
                "            timeout=httpx.Timeout(\n                self.timeout,\n                connect=min(_CONNECT_TIMEOUT_DEFAULT, self.timeout),\n                read=self.timeout,\n                write=self.timeout,\n                pool=self.timeout,\n            ),",
                "            timeout=self.timeout,",
            )
        ],
        ["tests/common/test_upstream_config.py::test_所有上游client都有独立连接超时"],
    ),
    (
        "M9: 唯一真相源绑死 get_config（打桩打歪 ⇒ 用例读真 DB）",
        "common/upstream_config.py",
        [
            (
                "        stored = await system_config.get_config(key)",
                "        stored = await _BOUND_GET_CONFIG(key)",
            ),
            (
                "from stashbox.backend.common.system_config import KEY_LLM, KEY_TTS",
                "from stashbox.backend.common.system_config import KEY_LLM, KEY_TTS\n"
                "from stashbox.backend.common.system_config import get_config as _BOUND_GET_CONFIG",
            ),
        ],
        ["tests/common/test_upstream_config.py::test_唯一真相源不得绑死被测函数"],
    ),
    (
        "M10: tier_router 退回读 DB 原始行（平行实现 ⇒ 静默不跟随）",
        "ai-service/distill/tier_router.py",
        [
            (
                "        from stashbox.backend.common.upstream_config import peek_llm\n\n        llm_cfg, _source = await peek_llm()",
                "        from stashbox.backend.common.system_config import KEY_LLM, get_config\n\n"
                "        llm_cfg = await get_config(KEY_LLM) or {}",
            )
        ],
        ["tests/common/test_upstream_config.py::test_消费方必须走统一解析器"],
    ),
    (
        "M11: admin 测试调用端点退回读原始行取 key（专属字段读不到）",
        "content-service/admin_router.py",
        [
            (
                "        stored_cfg, _src = await peek_llm()",
                "        stored_cfg = await system_config.get_config(system_config.KEY_LLM) or {}",
            )
        ],
        ["tests/common/test_upstream_config.py::test_消费方必须走统一解析器"],
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

        # 还原后必须绿
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
