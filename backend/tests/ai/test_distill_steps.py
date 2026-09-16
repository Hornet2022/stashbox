"""蒸馏 4 步函数单测（任务包 §4.1）。

全部用 FakeLLM / MockLLMClient + MockTTSClient，不接真 LLM / TTS。
"""
import json

import pytest

from distill import (
    MockTTSClient,
    TTSOutput,
    step1_structure,
    step2_rewrite,
    step3_tts,
    step4_concat,
)
from distill.prompts import STEP1_SYSTEM, STEP2_SYSTEM, STEP3_USER
from distill.schemas import (
    AudioConcatOutput,
    RewriteOutput,
    StructuredChapter,
    StructuredOutput,
)
from llm import MockLLMClient

STRUCTURED_JSON = json.dumps(
    {
        "summary": "AI 行业三件大事",
        "chapters": [
            {"title": "模型降价", "summary": "推理成本暴跌", "key_points": ["降价 90%"]},
            {"title": "Agent 爆发", "summary": "应用层起飞", "key_points": ["工具调用", "长上下文"]},
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


async def test_step1_sends_system_and_user_messages(ctx, fake_llm_cls):
    llm = fake_llm_cls(STRUCTURED_JSON)

    await step1_structure(ctx, llm)

    req = llm.requests[0]
    assert req.messages[0].role == "system"
    assert req.messages[0].content == STEP1_SYSTEM
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
    assert ctx.structured.tags == ["mock-tag-1", "mock-tag-2"]


async def test_step1_coerces_string_chapters_from_mock_client(ctx):
    """MockLLMClient 的 chapters 是字符串数组 —— 也要能装进 StructuredChapter。"""
    ctx.raw_content = "请把这篇文章结构化"  # 命中 MockLLMClient 的「结构化」路由
    llm = MockLLMClient(latency_ms=0)

    await step1_structure(ctx, llm)

    assert isinstance(ctx.structured.chapters[0], StructuredChapter)
    assert ctx.structured.summary == "mock summary"
    assert "AI" in ctx.structured.entities
    await llm.close()


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
    assert ctx.rewrite.word_count == len(ctx.rewrite.body)


async def test_step2_with_mock_llm_client(ctx):
    await step1_structure(ctx, MockLLMClient(latency_ms=0))
    llm = MockLLMClient(latency_ms=0)

    await step2_rewrite(ctx, llm)

    assert ctx.rewrite.hook
    assert ctx.rewrite.body
    await llm.close()


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------
async def test_step3_requires_step2_first(ctx):
    with pytest.raises(ValueError, match="rewrite not set"):
        await step3_tts(ctx, MockTTSClient())


async def test_step3_with_mock_tts_client(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))

    await step3_tts(ctx, MockTTSClient())

    assert isinstance(ctx.tts, TTSOutput)
    assert len(ctx.tts.segments) == 2
    assert ctx.tts.segments[0]["audio_url"] == "https://mock/seg1.m4a"
    assert ctx.tts.segments[1]["voice"] == "zh_female_lively"


async def test_step3_passes_rewrite_body_to_tts(ctx, fake_llm_cls):
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

    assert tts.prompts[0] == STEP3_USER.format(rewrite_body=ctx.rewrite.body)
    assert ctx.tts.segments[0]["audio_url"] == "https://mock/a.m4a"


# ---------------------------------------------------------------------------
# Step 4
# ---------------------------------------------------------------------------
async def test_step4_requires_step3_first(ctx):
    with pytest.raises(ValueError, match="tts not set"):
        await step4_concat(ctx)


async def test_step4_builds_final_audio(ctx, fake_llm_cls):
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))
    await step3_tts(ctx, MockTTSClient())

    await step4_concat(ctx)

    assert isinstance(ctx.final, AudioConcatOutput)
    assert ctx.article_id in ctx.final.audio_url
    assert ctx.final.audio_url.endswith(".m4a")
    assert ctx.final.duration_sec == 1800
    assert ctx.final.format == "m4a"


async def test_full_chain_populates_every_stage(ctx, fake_llm_cls):
    """4 步顺序执行后，context 上 4 个中间结果都在。"""
    await step1_structure(ctx, fake_llm_cls(STRUCTURED_JSON))
    await step2_rewrite(ctx, fake_llm_cls(REWRITE_JSON))
    await step3_tts(ctx, MockTTSClient())
    await step4_concat(ctx)

    assert ctx.structured is not None
    assert ctx.rewrite is not None
    assert ctx.tts is not None
    assert ctx.final is not None
