#!/usr/bin/env python3
"""mutation_tts_preset.py —— CP-TTS-PRESET 守卫的变异测试（验证守卫有牙齿）。

每个变异把实现改坏一处，跑对应守卫，**期望它变红**；随后立刻还原。
任何变异「守卫仍绿」= 那条守卫没牙齿（或判据打错了层）。

用法：
    cd backend && CI=true .venv/bin/python scripts/mutation_tts_preset.py

纪律（仓内既有）：
* 锚点 old_string 必须在目标文件**唯一命中**（命中 >1 会抛错，需加上下文消歧）。
* 改完真跑一遍，看每个变异是否真被抓住，而不是只跑守卫测试。
* 全程只改源码副本意义上的「改→跑→还原」，结束态与起始态逐字节一致。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TOOLS = BACKEND / "ai-service" / "agent" / "tools.py"
OPENAI = BACKEND / "app" / "services" / "tts" / "openai.py"

# (名称, 目标文件, old_string, new_string, 对应守卫)
MUTATIONS = [
    (
        "M1 预设路径误带 ref（退回克隆采样）",
        TOOLS,
        "audio = await client.synthesize(script, voice=resolved.preset_voice)",
        "audio = await client.synthesize(script, voice=resolved.preset_voice, "
        "ref_audio=resolved.ref_audio, ref_text=resolved.ref_text)",
        "tests/ai/test_tts_voice_preset.py::test_预设路径只带voice不带ref",
    ),
    (
        "M2 预设生效却 applied=False（溯源说谎）",
        TOOLS,
        "            applied = True\n        else:",
        "            applied = False\n        else:",
        "tests/ai/test_tts_voice_preset.py::test_预设溯源诚实记录",
    ),
    (
        "M3 use_preset 漏掉 provider 判定（indextts 误走预设）",
        TOOLS,
        'use_preset = bool(resolved.preset_voice) and client.provider_name == "openai"',
        "use_preset = bool(resolved.preset_voice)",
        "tests/ai/test_tts_voice_preset.py::test_非openai声明预设回落克隆",
    ),
    (
        "M4 openai 一律走预设（克隆音色被误切预设）",
        TOOLS,
        'use_preset = bool(resolved.preset_voice) and client.provider_name == "openai"',
        'use_preset = client.provider_name == "openai"',
        "tests/ai/test_tts_voice_preset.py::test_openai克隆音色无preset走克隆",
    ),
    (
        "M5 预设块长被克隆块长夹（普通预算失效）",
        OPENAI,
        "if clone is not None and self.clone_chunk_chars > 0:",
        "if self.clone_chunk_chars > 0:",
        "tests/tts/test_tts_voice_preset_body.py::test_预设走普通块长预算不被克隆夹",
    ),
]


def _run_guard(target: str) -> bool:
    """跑单条守卫，返回是否通过（True=绿）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    print("═══ CP-TTS-PRESET 变异测试 ═══")
    # 先确认基线全绿（否则变异结果无意义）
    baseline_targets = [m[4] for m in MUTATIONS]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *baseline_targets,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("❌ 基线不绿，先修守卫再跑变异：\n" + proc.stdout[-2000:])
        return 2
    print("基线：5 条守卫全绿 ✓\n")

    caught = 0
    for name, path, old, new, target in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        hits = src.count(old)
        if hits != 1:
            print(f"❌ {name}: 锚点命中 {hits} 次（须唯一），跳过 —— 加上下文消歧")
            continue
        mutated = src.replace(old, new)
        path.write_text(mutated, encoding="utf-8")
        try:
            green = _run_guard(target)
        finally:
            path.write_text(src, encoding="utf-8")  # 无论如何还原
        if green:
            print(f"❌ {name}: 守卫仍绿 ⇒ 没牙齿（判据可能打错层）")
        else:
            caught += 1
            print(f"✓ {name}: 守卫变红 ⇒ 抓住")

    # 还原校验：结束态须与起始态一致
    print(f"\n═══ 结果：{caught}/{len(MUTATIONS)} 变异被抓住 ═══")
    return 0 if caught == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
