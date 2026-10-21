"""两阶段改写：分段读取 → 基于素材写稿。

## 为什么有这个设计

上游 vLLM 会**随机**把生成内容整个丢掉（HTTP 200、content 为空、
completion_tokens > 0），且**与 prompt 长度强相关**（实测 2026-10-21）：

    user 735 字  → 50% 空
    user 1465 字 → 100% 空
    user 2925 字 → 100% 空

库里 2920 字那篇文章连续 4 次尝试全空，800 字那篇一次就过。

所以「把长文一次塞进 prompt」对长文根本不可用。但单纯「分段独立改写」
也不够 —— 每块只看局部原文，产不出跨段的关联思考与拓展方向。

于是拆成两步：**读取**（分段，每段短）→ **写稿**（输入=全部素材，短）。
阶段 ② 看到全文骨架，所以能写全局性的关联与拓展。
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


@pytest.fixture(scope="module")
def chunking():
    import importlib

    if "agent" not in sys.modules:
        importlib.import_module("agent")
    return importlib.import_module("agent.rewrite_chunking")


@pytest.fixture(scope="module")
def runner():
    import importlib

    if "agent" not in sys.modules:
        importlib.import_module("agent")
    return importlib.import_module("agent.runner")


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


def test_short_content_is_one_chunk(chunking):
    assert chunking.split_for_rewrite("很短的一段。", 500) == ["很短的一段。"]


def test_each_chunk_respects_limit(chunking):
    text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(20))
    chunks = chunking.split_for_rewrite(text, 500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks), [len(c) for c in chunks]


def test_split_keeps_all_content(chunking):
    """切分不能丢字（丢了就是内容缺失）。"""
    text = "\n\n".join(f"第{i}段内容{i}。" for i in range(50))
    chunks = chunking.split_for_rewrite(text, 100)
    joined = "".join(chunks)
    for i in range(50):
        assert f"第{i}段内容{i}。" in joined, f"第 {i} 段丢了"


def test_long_paragraph_is_split_by_sentence(chunking):
    """单段超长 → 按句读边界切，不硬切到词中间。"""
    para = "这是一句话。这是另一句话。这是第三句话。"
    long_para = para * 40
    chunks = chunking.split_for_rewrite(long_para, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)


def test_target_chars_grows_with_source_length(chunking):
    """目标字数随原文长度增长（原来写死在 system prompt 里）。"""
    lo_s, hi_s = chunking.target_chars_for(300)
    lo_l, hi_l = chunking.target_chars_for(3000)
    assert lo_l > lo_s
    assert hi_l > hi_s


# ---------------------------------------------------------------------------
# 素材解析
# ---------------------------------------------------------------------------


def test_parse_extract_reads_all_four_fields(runner):
    raw = (
        '{"key_points": ["要点一", "要点二"], "insights": ["思考一"], '
        '"related": ["关联一"], "follow_ups": ["拓展一"]}'
    )
    mat = runner._parse_extract(raw)
    assert mat.key_points == ["要点一", "要点二"]
    assert mat.insights == ["思考一"]
    assert mat.related == ["关联一"]
    assert mat.follow_ups == ["拓展一"]
    assert mat.degraded is False


def test_parse_extract_handles_string_instead_of_list(runner):
    """LLM 把数组写成字符串时也要能解析。"""
    mat = runner._parse_extract('{"key_points": "单个要点", "insights": []}')
    assert mat.key_points == ["单个要点"]
    assert mat.insights == []


def test_parse_extract_degrades_to_whole_text(runner):
    """不是 JSON → 整段当要点（降级但不崩）。"""
    mat = runner._parse_extract("就是一段普通文字，没有 JSON。")
    assert mat.degraded is True
    assert mat.key_points == ["就是一段普通文字，没有 JSON。"]


def test_parse_extract_empty_is_empty(runner):
    mat = runner._parse_extract("")
    assert mat.is_empty() is True


def test_material_render_has_sections(runner):
    mat = runner._ExtractedMaterial(
        key_points=["要点"],
        insights=["思考"],
        related=["关联"],
        follow_ups=["拓展"],
    )
    text = mat.render()
    assert "核心要点" in text
    assert "关联思考" in text
    assert "值得关联" in text
    assert "可拓展关注" in text


# ---------------------------------------------------------------------------
# 两阶段编排
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_stage_extracts_per_chunk_then_writes(runner, monkeypatch):
    """长文 → 分段抽取 → 一次写稿。写稿只发生一次。"""
    calls: list[tuple[str, str]] = []

    async def _fake_chat(llm, prompt, system_prompt=None, step=None, task_id=None):
        calls.append((step, system_prompt or ""))
        if step == "agent_extract":
            return '{"key_points": ["要点"], "insights": ["思考"], "related": [], "follow_ups": []}'
        return '{"hook": "钩子", "sections": ["节拍"], "outro": "收束"}'

    monkeypatch.setattr(runner, "_chat_text", _fake_chat)

    text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(20))
    out = await runner._rewrite_two_stage(object(), text, state={})

    extract_calls = [c for c in calls if c[0] == "agent_extract"]
    write_calls = [c for c in calls if c[0] == "agent_rewrite"]
    assert len(extract_calls) > 1, "长文应分段抽取"
    assert len(write_calls) == 1, "写稿只做一次（基于全部素材）"
    # 阶段 ② 的 system 是写稿契约，阶段 ① 是抽取契约
    assert "播客主理人" in write_calls[0][1]
    assert "素材编辑" in extract_calls[0][1]
    assert "钩子" in out


@pytest.mark.asyncio
async def test_two_stage_raises_when_all_extractions_empty(runner, monkeypatch):
    """所有段都抽不到素材 → 抛错（不能拿空素材写出一篇空稿）。"""

    async def _fake_chat(llm, prompt, system_prompt=None, step=None, task_id=None):
        return "" if step == "agent_extract" else '{"hook": "x", "sections": ["y"]}'

    monkeypatch.setattr(runner, "_chat_text", _fake_chat)

    text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(20))
    with pytest.raises(Exception):
        await runner._rewrite_two_stage(object(), text, state={})


@pytest.mark.asyncio
async def test_two_stage_includes_source_for_short_content(runner, monkeypatch):
    """短文 → 阶段 ② 额外带上原文全文（细节不丢）。"""
    prompts: list[str] = []

    async def _fake_chat(llm, prompt, system_prompt=None, step=None, task_id=None):
        prompts.append(prompt)
        if step == "agent_extract":
            return '{"key_points": ["要点"]}'
        return '{"hook": "钩子", "sections": ["节拍"]}'

    monkeypatch.setattr(runner, "_chat_text", _fake_chat)

    short = "很短的原文。"
    await runner._rewrite_two_stage(object(), short, state={})

    write_prompt = [
        p for p, s in zip(prompts, ["agent_extract", "agent_rewrite"]) if s == "agent_rewrite"
    ]
    assert write_prompt, "应有写稿 prompt"
    assert "原文全文" in write_prompt[0]
    assert "素材包" in write_prompt[0]


@pytest.mark.asyncio
async def test_two_stage_omits_source_for_long_content(runner, monkeypatch):
    """长文 → 阶段 ② 不带原文全文（否则又变长 prompt，回到丢内容的老问题）。"""
    prompts: list[str] = []
    steps: list[str] = []

    async def _fake_chat(llm, prompt, system_prompt=None, step=None, task_id=None):
        prompts.append(prompt)
        steps.append(step or "")
        if step == "agent_extract":
            return '{"key_points": ["要点"]}'
        return '{"hook": "钩子", "sections": ["节拍"]}'

    monkeypatch.setattr(runner, "_chat_text", _fake_chat)

    long_text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(20))
    await runner._rewrite_two_stage(object(), long_text, state={})

    write_prompt = prompts[steps.index("agent_rewrite")]
    assert "原文全文" not in write_prompt
    assert "素材包" in write_prompt
