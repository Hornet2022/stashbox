"""蒸馏 4 步函数单测（任务包 §4.1）。

全部用 FakeLLM / FakeTTSClient 单测替身，不接真 LLM / TTS（非 mock 回退）。
"""

import json

import pytest

from distill import (
    step1_structure,
    step2_rewrite,
    step3_tts,
    step4_concat,
)
from distill.prompts import STEP1_SYSTEM, STEP2_SYSTEM, STEP3_USER
from distill.schemas import (
    AudioConcatOutput,
    RewriteOutput,
    StructuredOutput,
)

STRUCTURED_JSON = json.dumps(
    {
        "summary": "AI 行业三件大事",
        "chapters": [
            {"title": "模型降价", "summary": "推理成本暴跌", "key_points": ["降价 90%"]},
            {
                "title": "Agent 爆发",
                "summary": "应用层起飞",
                "key_points": ["工具调用", "长上下文"],
            },
        ],
        "entities": ["OpenAI", "Anthropic"],
        "tags": ["AI", "商业"],
    },
    ensure_ascii=False,
)

REWRITE_JSON = json.dumps(
    {
        "hook": "你可能没想到，AI 降价比你想象得快。",
        "body": "咱们今天聊三件事...",
        "outro": "总结一下，别忘了行动。",
        "word_count": 8200,
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# Step 1
# ---------------------------------------------------------------------------
async def test_step1_parses_json_into_structured_output(ctx, fake_llm_cls):
    llm = fake_llm_cls(STRUCTURED_JSON)

    await step1_structure(ctx, llm)

    assert isinstance(ctx.structured, StructuredOutput)
    assert ctx.structured.summary == "AI 行业三件大事"
    assert len(ctx.structured.chapters) == 2
    assert ctx.structured.chapters[0].title == "模型降价"
    assert ctx.structured.chapters[0].key_points == ["降价 90%"]
    assert ctx.structured.entities == ["OpenAI", "Anthropic"]
    assert ctx.structured.tags == ["AI", "商业"]


async def test_step1_sends_system_and_user_messages(ctx, fake_llm_cls, monkeypatch):
    """CP3.6.2 fixture 同步：step1_structure 会调 STEP1_SYSTEM.format(tag_vocabulary=...)。

    这里 mock `_load_tag_vocabulary` 返回固定值，让 system_content 可断言。
    """
    from unittest.mock import AsyncMock

    from distill import steps as steps_module

    monkeypatch.setattr(
        steps_module,
        "_load_tag_vocabulary",
        AsyncMock(return_value="时事、生活、科技、财经"),
    )

    llm = fake_llm_cls(STRUCTURED_JSON)

    await step1_structure(ctx, llm)

    req = llm.requests[0]
    assert req.messages[0].role == "system"
    # CP3.6.2: STEP1_SYSTEM 是含 `{tag_vocabulary}` 占位符的模板，
    # step1_structure 在运行时 format 注入实际词表。
    assert req.messages[0].content == STEP1_SYSTEM.format(tag_vocabulary="时事、生活、科技、财经")
    assert req.messages[1].role == "user"
    assert ctx.raw_content in req.messages[1].content
    assert req.metadata["step"] == "step1_structure"


async def test_step1_uses_untitled_placeholder_when_no_title(ctx, fake_llm_cls):
    ctx.title = None
    llm = fake_llm_cls(STRUCTURED_JSON)

    await step1_structure(ctx, llm)

    assert "(无标题)" in llm.requests[0].messages[1].content


async def test_step1_falls_back_to_mock_when_response_is_not_json(ctx, fake_llm_cls):
    llm = fake_llm_cls("这不是 JSON")

    await step1_structure(ctx, llm)

    assert ctx.structured.summary == "mock summary"
    assert ctx.structured.chapters[0].title == "章1"
    assert ctx.structured.tags == ["__mock__"]


# ---------------------------------------------------------------------------
# Step 2
# ---------------------------------------------------------------------------
async def test_step2_requires_step1_first(ctx, fake_llm_cls):
    with pytest.raises(ValueError, match="structured not set"):
        await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))


async def test_step2_parses_json_into_rewrite_output(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    llm = fake_llm_cls(REWRITE_JSON)

    await step2_rewrite(ctx, llm)

    assert isinstance(ctx.rewrite, RewriteOutput)
    assert ctx.rewrite.hook == "你可能没想到，AI 降价比你想象得快。"
    assert ctx.rewrite.body == "咱们今天聊三件事..."
    assert ctx.rewrite.outro == "总结一下，别忘了行动。"
    assert ctx.rewrite.word_count == 8200


async def test_step2_feeds_structured_json_into_prompt(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    llm = fake_llm_cls(REWRITE_JSON)

    await step2_rewrite(ctx, llm)

    req = llm.requests[0]
    assert req.messages[0].content == STEP2_SYSTEM
    assert "AI 行业三件大事" in req.messages[1].content  # 上一阶段的 JSON 进了 prompt


async def test_step2_splits_plain_text_into_hook_body_outro(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    llm = fake_llm_cls("开场钩子。\n主体第一段。\n主体第二段。\n结尾钩子。")

    await step2_rewrite(ctx, llm)

    assert ctx.rewrite.hook == "开场钩子。"
    assert ctx.rewrite.outro == "结尾钩子。"
    assert "主体第一段。" in ctx.rewrite.body
    assert "主体第二段。" in ctx.rewrite.body
    # CP-DQ word_count = hook + sections + outro 全字符数（不是 len(body)）。
    expected_wc = len(ctx.rewrite.hook) + len(ctx.rewrite.body) + len(ctx.rewrite.outro)
    assert ctx.rewrite.word_count == expected_wc


async def test_step2_with_fake_llm_client(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    llm = fake_llm_cls("改写稿正文")

    await step2_rewrite(ctx, llm)

    assert ctx.rewrite.hook
    assert ctx.rewrite.body


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------
async def test_step3_requires_step2_first(ctx, fake_tts_cls):
    with pytest.raises(ValueError, match="rewrite not set"):
        await step3_tts(ctx, fake_tts_cls())


async def test_step3_passes_rewrite_body_to_tts(ctx, fake_llm_cls):
    """CP9.x：step3_tts 直接把 ctx.rewrite 拆段喂给 TTS，不再套 STEP3_USER prompt。

    之前实现把"分 3-5 段, 每段 ≤ 30 秒"等元指令当台词读，合成时间/音频长度都异常。

    CP-DQ：现在 step3_tts 按 sections 切 TTS 段（hook + sections + outro），每段 ≤ 100 字符。
    测试只断言"所有 TTS 段拼回去 = body + hook + outro"，不要求单段等于 body。
    """
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))

    class RecordingTTS:
        def __init__(self):
            self.prompts = []

        async def synthesize(self, text: str) -> list[dict]:
            self.prompts.append(text)
            return [{"text": "段1", "voice": "v", "audio_url": "https://mock/a.m4a"}]

    tts = RecordingTTS()
    await step3_tts(ctx, tts)

    # CP-DQ 修复：TTS 收到的是分段（hook + sections + outro 拆分），不包含 STEP3_USER 模板
    # 不再断言 tts.prompts[0] == body，改断言"所有 prompt 拼起来 ≈ 全文"
    joined = "".join(tts.prompts)
    expected_full = ctx.rewrite.hook + "".join(ctx.rewrite.sections) + ctx.rewrite.outro
    assert joined == expected_full
    # 不带元指令
    assert "STEP3_USER" not in joined
    assert "分 3-5 段" not in joined
    assert STEP3_USER not in tts.prompts[0]  # 不能包含"分 3-5 段..."等元指令
    assert ctx.tts.segments[0]["audio_url"] == "https://mock/a.m4a"


# ---------------------------------------------------------------------------
# Step 4
# ---------------------------------------------------------------------------
async def test_step4_requires_step3_first(ctx):
    with pytest.raises(ValueError, match="tts not set"):
        await step4_concat(ctx)


async def test_step4_builds_final_audio(ctx, fake_llm_cls, fake_tts_cls):
    """step4 真实拼接路径：用 FakeTTSClient 产真 WAV bytes。

    audio_url 在 step4 阶段仍留空（交给 _save_audio 决定真实 URL），
    audio_bytes 非空、duration_sec 由真实 WAV 头解析得出 > 0。
    """
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))
    await step3_tts(ctx, fake_tts_cls())

    await step4_concat(ctx)

    assert isinstance(ctx.final, AudioConcatOutput)
    assert ctx.final.audio_url == ""  # step4 留空，等 _save_audio 上传后覆盖
    assert ctx.final.audio_bytes is not None  # 真实 bytes，非 mock 占位
    assert ctx.final.duration_sec > 0  # 真实时长，不再硬编码 0
    assert ctx.final.format in ("wav", "m4a")


async def test_full_chain_populates_every_stage(ctx, fake_llm_cls, fake_tts_cls):
    """4 步顺序执行后，context 上 4 个中间结果都在。"""
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))
    await step3_tts(ctx, fake_tts_cls())
    await step4_concat(ctx)

    assert ctx.structured is not None
    assert ctx.rewrite is not None
    assert ctx.tts is not None
    assert ctx.final is not None
