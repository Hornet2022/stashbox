"""CP-AGENT-REWRITE-STRUCTURED：改写输出的结构化契约（2026-10-02 端到端自测新增）。

## 背景：闭环 1 在自我污染

下游 `evaluation_service.submit_user_evaluation` 用
`script_text.split("\\n\\n", 1)[0]` 取 hook 存进 few-shot 池喂给后续改写。

agent 的 prompt 曾经是一句「改写成听感稿」，让 LLM 自由输出纯文本，
于是 script_text 实际长这样（生产真样本，2026-10-02）：

    （轻松开场音乐淡出）

    哈喽各位正在通勤路上的朋友，今天咱们来聊聊一款充电宝……

`split("\\n\\n")[0]` 拿到的是**那行音效标注**。用户给优质开场白打了 4 分，
系统把音效标注当成「高分改写范例」存进池子。实测池里当时唯一一条就是
`（轻松开场音乐淡出）`，而它配的 input_excerpt 是个 sha256 hash。

修法是让 LLM 回 JSON {hook, sections, outro}（恢复 `distill/prompts.py`
早就定义过、agent 换 prompt 时丢掉的契约），由代码拼整稿并保证段落边界。
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _first_para(script: str) -> str:
    """复刻 evaluation_service.py:92 的取法。"""
    return script.split("\n\n", 1)[0].strip()


# ---------------------------------------------------------------------------
# 1. 正常 JSON 路径
# ---------------------------------------------------------------------------


def test_parses_wellformed_json():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(
        '{"hook": "哈喽各位通勤的朋友", "sections": ["第一节。", "第二节。"], "outro": "今天回家可以想想这事。"}'
    )

    assert p.degraded is False
    assert p.hook == "哈喽各位通勤的朋友"
    assert p.sections == ["第一节。", "第二节。"]
    assert p.outro == "今天回家可以想想这事。"


def test_first_para_of_script_is_exactly_the_hook():
    """**这条是整个修复的核心断言**。

    入池取的是整稿第一段，所以第一段必须严格等于 hook —— 不是"包含 hook"，
    不是"开头像 hook"，是相等。音效标注混进第一段 = 池子被污染。
    """
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(
        '{"hook": "哈喽各位通勤的朋友", "sections": ["第一节内容。", "第二节内容。"], "outro": "收束。"}'
    )

    assert _first_para(p.script) == p.hook == "哈喽各位通勤的朋友"


def test_script_preserves_all_three_parts():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('{"hook": "H", "sections": ["S1", "S2"], "outro": "O"}')

    assert p.script == "H\n\nS1\n\nS2\n\nO"


def test_hook_with_internal_blank_lines_stays_one_paragraph():
    """hook 内部有换行也不能破坏段落边界。

    否则 `split("\\n\\n")[0]` 拿到的只是 hook 的前半截。
    """
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('{"hook": "第一行\\n\\n第二行", "sections": ["正文"], "outro": "O"}')

    assert _first_para(p.script) == "第一行\n第二行"
    assert "第一行" in p.script and "第二行" in p.script


# ---------------------------------------------------------------------------
# 2. 真实污染样本（降级路径）
# ---------------------------------------------------------------------------


PRODUCTION_POLLUTED_SAMPLE = (
    "（轻松开场音乐淡出）\n\n"
    "哈喽各位正在通勤路上的朋友，今天咱们来聊聊一款充电宝——酷态科10号，"
    "正好最近我拿它搭配Flipgo出门用。\n\n"
    "最开始为什么选这款充电宝呢？其实主要有两个原因。\n\n"
    "今天就聊到这，掰掰。"
)


def test_real_polluted_sample_hook_is_not_the_sfx_line():
    """真实生产样本：降级后 hook 必须是正文，**不能是音效标注**。"""
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(PRODUCTION_POLLUTED_SAMPLE)

    assert _first_para(p.script) != "（轻松开场音乐淡出）"
    assert _first_para(p.script).startswith("哈喽各位正在通勤路上的朋友")


def test_sfx_paragraph_is_not_poolable_at_all():
    """音效标注整段不能出现在任何可被入池取到的位置。"""
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(PRODUCTION_POLLUTED_SAMPLE)

    for para in p.script.split("\n\n"):
        assert "轻松开场音乐淡出" not in para


def test_sfx_only_paragraphs_are_stripped():
    from agent.runner import _is_sfx_para

    assert _is_sfx_para("（轻松开场音乐淡出）")
    assert _is_sfx_para("【片头音乐】")
    assert _is_sfx_para("[Background music fades]")
    # 正常正文不能被误杀
    assert not _is_sfx_para("哈喽各位正在通勤路上的朋友")
    assert not _is_sfx_para("（这个充电宝的续航实测是 12 小时 34 分，比官方标称的 10 小时还长）")


def test_degraded_flag_is_set_on_fallback():
    from agent.runner import _parse_rewrite

    assert _parse_rewrite(PRODUCTION_POLLUTED_SAMPLE).degraded is True


# ---------------------------------------------------------------------------
# 3. LLM 不听话的各种形态
# ---------------------------------------------------------------------------


def test_strips_markdown_fence():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('```json\n{"hook": "钩子", "sections": ["节"], "outro": "O"}\n```')

    assert p.degraded is False
    assert p.hook == "钩子"


def test_tolerates_trailing_comma():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('{"hook": "钩子", "sections": ["节",], "outro": "O",}')

    assert p.degraded is False
    assert p.hook == "钩子"


def test_tolerates_prose_around_json():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(
        '好的，改写如下：\n{"hook": "钩子", "sections": ["节"], "outro": "O"}\n希望对你有帮助。'
    )

    assert p.degraded is False
    assert p.hook == "钩子"


def test_sections_as_bare_string_is_accepted():
    """LLM 偶尔把 sections 写成单个字符串而不是数组。"""
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('{"hook": "钩子", "sections": "唯一一节", "outro": "O"}')

    assert p.degraded is False
    assert p.sections == ["唯一一节"]


def test_missing_hook_falls_back_to_first_section():
    """LLM 漏了 hook → 拿第一节顶上，别让整稿没有钩子。"""
    from agent.runner import _parse_rewrite

    p = _parse_rewrite('{"sections": ["第一节当钩子", "第二节"], "outro": "O"}')

    assert p.hook == "第一节当钩子"
    assert p.sections == ["第二节"]
    assert _first_para(p.script) == "第一节当钩子"


def test_empty_output_yields_empty_parts():
    from agent.runner import _parse_rewrite

    p = _parse_rewrite("")

    assert p.script == ""
    assert p.hook == ""


def test_never_raises_on_garbage():
    """解析器是兜底路径，任何输入都不能抛 —— 抛了整篇蒸馏就 failed。"""
    from agent.runner import _parse_rewrite

    for garbage in [
        "not json at all",
        "{broken json",
        "{{{",
        "null",
        "[1,2,3]",
        '{"hook": null, "sections": null, "outro": null}',
        "🎵🎵🎵",
    ]:
        p = _parse_rewrite(garbage)
        assert isinstance(p.script, str)


def test_prompt_forbids_sfx_annotations():
    """system prompt 必须明确禁止音效标注。

    只靠解析端兜底不够：LLM 少写一段音效 = 少一段正文。
    """
    from agent.runner import _REWRITE_SYSTEM

    assert "音效" in _REWRITE_SYSTEM
    assert "不要输出音效标注" in _REWRITE_SYSTEM
    # 结构化契约必须写在 prompt 里，否则 LLM 不知道要回什么
    assert '"hook"' in _REWRITE_SYSTEM
    assert '"sections"' in _REWRITE_SYSTEM
    assert '"outro"' in _REWRITE_SYSTEM
    assert "严格 JSON" in _REWRITE_SYSTEM


# ---------------------------------------------------------------------------
# 4. 与 agent 图的接线
# ---------------------------------------------------------------------------


def test_agent_state_declares_structured_rewrite_fields():
    """AgentState 少了字段 → 节点 return 的值会被 reducer 丢掉。"""
    from agent.state import AgentState

    for field in ("rewritten_script", "rewrite_hook", "rewrite_sections", "rewrite_outro"):
        assert field in AgentState.__annotations__, f"AgentState 缺 {field}"


def test_evaluation_service_hook_extraction_contract_holds():
    """端到端对齐：入池取法在这个新格式下确实拿到 hook。

    直接复刻 `evaluation_service.py` 的 `split("\\n\\n", 1)[0]`，
    保证以后谁改拼接顺序都会红。
    """
    from agent.runner import _parse_rewrite

    p = _parse_rewrite(
        '{"hook": "真正的高分开场白", "sections": ["正文一。", "正文二。"], "outro": "收束。"}'
    )

    # 这就是 evaluation_service.submit_user_evaluation 的入池文本
    pooled_text = _first_para(p.script)

    assert pooled_text == "真正的高分开场白"
    assert "（" not in pooled_text[:1], "hook 不该以音效括号开头"
