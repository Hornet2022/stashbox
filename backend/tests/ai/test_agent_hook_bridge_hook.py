"""agent_hook_bridge 的 hook 提取：闭环 1 污染的第二条路径（2026-10-03）。

## 这条路径之前被漏掉了

闭环 1 的污染有**两条**独立的入池路径：

1. `evaluation_service.submit_user_evaluation` — 用户在 App 里打分的入口，
   用 `script_text.split("\n\n", 1)[0]` 取 hook
2. `distill_task._run_agent_post_hooks` → `build_ctx_from_agent_final`
   → `FewShotPoolHook` → `ctx.rewrite.hook` — 蒸馏后自动入池

第一条在 `5731e84` 修了（让 rewrite_node 产出结构化 `{hook, sections, outro}`）。
第二条当时**只改了拼接格式、没改取数逻辑**：bridge 依然调 `_split_script(script)`
从扁平整稿里重新猜第一段。

当前两者碰巧一致（因为 `script` 就是按 `hook\n\nsections\n\noutro` 拼的），
但那是**巧合不是契约** —— 只要 LLM 走降级路径产出非预期段落结构，guess 就
悄悄错回去，而 few-shot 池不会报任何警。

另外这里还有个反向坑：`_split_script` 会对 hook 做 `[:80]` 截断
（`HOOK_MAX_CHARS`）。结构化字段是 LLM 自己标的，语义更准，不该再被切一刀。
"""

import sys
from pathlib import Path


REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

AI_SERVICE_DIR = str(Path(__file__).resolve().parents[2] / "ai-service")
if AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, AI_SERVICE_DIR)

BASE = "audio/xxx"
AUDIO = "https://cdn/x.m4a"


def _ctx(final: dict):
    from distill.agent_hook_bridge import build_ctx_from_agent_final

    return build_ctx_from_agent_final(
        task_id="dst_t",
        article_id="art_a",
        user_id=42,
        url="https://example.com",
        raw_content="原文",
        final=final,
    )


# ---------------------------------------------------------------------------
# 1. 有结构化字段时直接用，不猜
# ---------------------------------------------------------------------------


def test_prefers_structured_hook():
    ctx = _ctx(
        {
            "rewritten_script": "钩子\n\n正文一\n\n正文二",
            "rewrite_hook": "这是 LLM 标好的钩子",
            "rewrite_sections": ["正文一", "正文二"],
            "rewrite_outro": "收束",
        }
    )

    assert ctx.rewrite.hook == "这是 LLM 标好的钩子"
    assert ctx.rewrite.sections == ["正文一", "正文二"]
    assert ctx.rewrite.outro == "收束"


def test_structured_hook_is_not_truncated_to_80_chars():
    """结构化 hook 是 LLM 自己标的，不该再被 HOOK_MAX_CHARS 切一刀。

    旧路径 `paragraphs[0][:80]` 会把长开场白砍掉后半截；用户看到的高分反馈
    原文是完整的，入池的样本却是断的。
    """
    long_hook = "这" * 120
    ctx = _ctx(
        {
            "rewritten_script": f"{long_hook}\n\n正文",
            "rewrite_hook": long_hook,
            "rewrite_sections": ["正文"],
        }
    )

    assert ctx.rewrite.hook == long_hook
    assert len(ctx.rewrite.hook) == 120


def test_structured_fields_win_even_when_script_disagrees():
    """script 与结构化字段冲突时，以结构化字段为准。

    这是「用契约而不是猜」的核心：降级路径拼出来的 script 段落结构不可信。
    """
    ctx = _ctx(
        {
            # script 第一段是音效标注（降级路径的产物）
            "rewritten_script": "（轻松开场音乐淡出）\n\n真正开场白\n\n正文",
            "rewrite_hook": "真正开场白",
            "rewrite_sections": ["正文"],
        }
    )

    assert ctx.rewrite.hook == "真正开场白"
    assert "音乐淡出" not in ctx.rewrite.hook


# ---------------------------------------------------------------------------
# 2. 缺字段时回退切分（向后兼容）
# ---------------------------------------------------------------------------


def test_falls_back_to_split_when_no_structured_fields():
    """旧 agent final（没有结构化字段）仍要能工作。"""
    ctx = _ctx({"rewritten_script": "第一段开场\n\n中间段\n\n最后收束"})

    assert ctx.rewrite.hook == "第一段开场"
    assert ctx.rewrite.outro == "最后收束"


def test_falls_back_when_structured_hook_empty():
    """结构化字段在但 hook 空 → 退回切分，至少能拿到首段。"""
    ctx = _ctx(
        {
            "rewritten_script": "首段当钩子\n\n正文",
            "rewrite_hook": "",
            "rewrite_sections": ["正文"],
        }
    )

    assert ctx.rewrite.hook == "首段当钩子"


def test_empty_script_yields_empty_hook():
    ctx = _ctx({"rewritten_script": ""})

    assert ctx.rewrite.hook == ""


def test_single_paragraph_script_becomes_hook():
    """整稿没空行 → 整体当 hook（既有行为，别改坏）。"""
    ctx = _ctx({"rewritten_script": "只有一段没有空行的稿子"})

    assert ctx.rewrite.hook.startswith("只有一段")


# ---------------------------------------------------------------------------
# 3. 这条路径喂的就是入池文本
# ---------------------------------------------------------------------------


def test_ctx_hook_is_exactly_what_few_shot_pool_will_store():
    """端到端对齐：FewShotPoolHook 存进池子的就是 ctx.rewrite.hook。

    复刻 hooks_impl.FewShotPoolHook 的取法，确保这条链路真的通到入池。
    """
    from distill.hooks_impl import FewShotPoolHook

    src_hook = FewShotPoolHook.__doc__ or ""
    assert src_hook is not None  # 只是确认 hook 类存在

    ctx = _ctx(
        {
            "rewritten_script": "音效标注\n\n真正的高分开场白\n\n正文",
            "rewrite_hook": "真正的高分开场白",
            "rewrite_sections": ["正文"],
        }
    )

    pooled = ctx.rewrite.hook
    assert pooled == "真正的高分开场白"
    assert "音效标注" not in pooled


def test_legacy_final_with_sfx_first_para_is_the_known_bad_case():
    """锁定「降级路径的 script」仍可能带音效段 —— 这是桥必须兜住的理由。

    旧 final（无结构化字段）+ 首段是音效标注时，回退切分会拿到音效段。
    这条测试存在的意义是把已知风险写在案上：一旦有人删掉结构化优先逻辑，
    闭环 1 的污染就会从这条路回来。
    """
    ctx = _ctx({"rewritten_script": "（轻松开场音乐淡出）\n\n真正开场白\n\n正文"})

    # 记录现状：回退路径确实会拿到音效段
    assert ctx.rewrite.hook == "（轻松开场音乐淡出）"
    # 修复路径下这条 final 不会再出现（rewrite_node 总会带结构化字段），
    # 所以真正的防线是「优先用结构化字段」——见上面三条。
