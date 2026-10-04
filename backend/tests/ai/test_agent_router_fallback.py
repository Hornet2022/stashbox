"""决策路由器的兜底逻辑（runner.decision_router_node）。

## 这条守卫防的是什么

`AGENT_ROUTER_MODE=llm` 分支里原本有这四行：

    else:
        try:
            raw = await _chat_text(...)
            action = _parse_next_action(raw)
            ...
        except Exception as exc:
            log.warning(...)
        action = default          # <- 缩进在 try/except 之后，无条件执行

最后那行 `action = default` 的缩进和 `try:` / `except:` **同级**，
所以它在 try/except 结束后**必然执行**，把上面 LLM 解析出来的
`action` 直接覆盖掉。

结果：`AGENT_ROUTER_MODE=llm` 变成"花 2~5s 时间和真实 token，
换一个一定会被丢弃的结果"，而 `runner.py:438` 的注释还写着
"想恢复 LLM 自主决策的实验行为，设 AGENT_ROUTER_MODE=llm 即可" ——
文档和行为的矛盾会让人以为功能是通的。

默认 `ROUTER_MODE=rules` 走 `if` 分支，永远进不了这个 `else`，
所以缺陷长期潜伏、无任何告警。

这类"多写了一行、把上面结果吃掉"的错误纯靠肉眼 review 极难发现：
代码看起来完全合理。所以用测试把"LLM 决策被采纳"这个意图钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

AI_SERVICE = Path("/Users/hornet/work/stashbox/backend/ai-service")
REPO_ROOT = Path("/Users/hornet/work/stashbox")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE))

from agent import runner  # noqa: E402


def _state() -> dict:
    return {
        "article_id": "art_test",
        "trace_id": "t1",
        "current_step": "rewrite",
        "status": "running",
        "tool_calls": [],
    }


@pytest.mark.asyncio
async def test_llm_decision_is_actually_used():
    """LLM 解析出的动作必须被采纳，不能被兜底覆盖。

    修复前这里会返回 `default`（因为无条件 `action = default` 覆盖了）。
    """
    with (
        patch.object(runner, "ROUTER_MODE", "llm"),
        patch.object(
            runner,
            "_chat_text",
            AsyncMock(return_value='{"next_action": "skip_to_tts", "reason": "内容够短"}'),
        ),
    ):
        out = await runner.decision_router_node(_state())

    assert (
        out["next_action"] == "skip_to_tts"
    ), f"LLM 的决策被覆盖了，实际拿到 {out.get('next_action')!r}"


@pytest.mark.asyncio
async def test_unparseable_llm_output_falls_back_to_default():
    """LLM 返回垃圾 → 必须退回 default（兜底仍然有效）。"""
    with (
        patch.object(runner, "ROUTER_MODE", "llm"),
        patch.object(runner, "_chat_text", AsyncMock(return_value="完全不是 JSON")),
    ):
        out = await runner.decision_router_node(_state())

    assert out["next_action"] in {"rewrite", "skip_to_tts", "skip_to_concat", "fail"}


@pytest.mark.asyncio
async def test_llm_exception_falls_back_to_default():
    """LLM 调用抛异常 → 必须退回 default（兜底仍然有效）。"""
    with (
        patch.object(runner, "ROUTER_MODE", "llm"),
        patch.object(runner, "_chat_text", AsyncMock(side_effect=RuntimeError("LLM 挂了"))),
    ):
        out = await runner.decision_router_node(_state())

    assert out["next_action"] in {"rewrite", "skip_to_tts", "skip_to_concat", "fail"}


@pytest.mark.asyncio
async def test_rules_mode_never_calls_llm():
    """默认 rules 模式不调 LLM —— 这是生产当前走的分支，必须零开销。"""
    chat = AsyncMock(return_value='{"next_action": "skip_to_tts"}')
    with patch.object(runner, "ROUTER_MODE", "rules"), patch.object(runner, "_chat_text", chat):
        out = await runner.decision_router_node(_state())

    chat.assert_not_called()
    assert out["next_action"] in {"rewrite", "skip_to_tts", "skip_to_concat", "fail"}
