#!/usr/bin/env python3
"""验证上游 LLM 是否接受 max_tokens=16384（2026-11-25）。

为什么需要验证：改写稿的 JSON 被 `max_tokens=8192` 截断 ⇒ 结构丢失
（已用「逐元素抢救」兜住，但那是**事后补救**）。能不能**从源头**把上限抬高？
必须实测 —— 上游不支持的话，请求会直接报错（整条蒸馏链路挂掉）。

用法（生产）：
    cd /opt/stashbox/stashbox/backend
    export PYTHONPATH=/opt/stashbox
    set -a && . /etc/stashbox/stashbox.env && set +a
    /opt/stashbox/venv/bin/python scripts/exp_llm_max_tokens.py

退出码：0 = 该上限可用；3 = 上游拒绝（不要上线这个上限）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_PARENT = _REPO_ROOT.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from verify_audio_consistency import _load_env_file  # noqa: E402

_load_env_file()

PROMPT = "请用中文写一段 1200 字的说明，介绍城市轨道交通的信号系统演进历史。"


async def _run() -> int:
    # 工厂在 ai-service 下（目录名带连字符 ⇒ 走 sys.path 别名）
    # ⚠️ `_REPO_ROOT` 是**包父目录**（parents[2]）—— ai-service 在
    # `parents[1]`（backend）下面，别拼错。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))
    from llm import factory
    from llm.types import ChatMessage, ChatRequest

    await factory.reload()
    llm = factory.get_llm_client()
    print(f"[LLM] provider={getattr(llm, 'provider_name', '?')} model={getattr(llm, 'model', '?')}")

    for limit in (8192, 16384, 32768):
        req = ChatRequest(
            messages=[ChatMessage(role="user", content=PROMPT)],
            max_tokens=limit,
            temperature=0.3,
        )
        try:
            resp = await llm.chat(req)
            n = len(resp.content or "")
            print(f"  max_tokens={limit:<6} → ✓ 成功，返回 {n} 字")
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  max_tokens={limit:<6} → ✗ 失败：{msg[:220]}")
            if limit >= 16384:
                print("\n结论：上游拒绝这个上限 ⇒ 不要上线它（保持 8192 + 事后抢救）。")
                return 3
    print("\n结论：上游接受更大的 max_tokens ⇒ 可以从源头减少截断。")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 跑不起来: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
