"""蒸馏 4 步函数（CP3.5-pre-2，v1 §5.2.2 - §5.2.5）。

每步接 LLMClient（Step 3 接 TTS client），输出中间结果写到 context。
LLM 响应按「JSON 优先、纯文本兜底」解析 —— mock client 与真 LLM 都能跑通。
"""
import json

from llm.types import ChatMessage, ChatRequest
from observability.decorators import trace_distill_step

from .prompts import STEP1_SYSTEM, STEP1_USER, STEP2_SYSTEM, STEP2_USER, STEP3_USER
from .schemas import (
    AudioConcatOutput,
    DistillContext,
    RewriteOutput,
    StructuredChapter,
    StructuredOutput,
    TTSOutput,
)

# 兜底结构化结果（LLM 没返回可解析 JSON 时用，对应 v1 §5.2.2 mock 输出）
_FALLBACK_SUMMARY = "mock summary"
_FALLBACK_ENTITIES = ["mock entity"]
_FALLBACK_TAGS = ["mock-tag-1", "mock-tag-2"]


def _load_json(content: str) -> dict | list | None:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return None


def _coerce_chapter(raw) -> StructuredChapter:
    """章节允许 dict（真 LLM）或 str（mock client 的 `chapters: ["intro", ...]`）。"""
    if isinstance(raw, dict):
        return StructuredChapter(
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            key_points=[str(p) for p in raw.get("key_points") or []],
        )
    return StructuredChapter(title=str(raw), summary="")


def _parse_structured(content: str) -> StructuredOutput:
    payload = _load_json(content)
    if isinstance(payload, dict):
        return StructuredOutput(
            summary=str(payload.get("summary") or _FALLBACK_SUMMARY),
            chapters=[_coerce_chapter(c) for c in payload.get("chapters") or []],
            entities=[str(e) for e in payload.get("entities") or _FALLBACK_ENTITIES],
            tags=[str(t) for t in payload.get("tags") or _FALLBACK_TAGS],
        )
    return StructuredOutput(
        summary=_FALLBACK_SUMMARY,
        chapters=[StructuredChapter(title="章1", summary=_FALLBACK_SUMMARY, key_points=[])],
        entities=list(_FALLBACK_ENTITIES),
        tags=list(_FALLBACK_TAGS),
    )


def _parse_rewrite(content: str) -> RewriteOutput:
    payload = _load_json(content)
    if isinstance(payload, dict) and {"hook", "body"} <= payload.keys():
        body = str(payload.get("body") or "")
        return RewriteOutput(
            hook=str(payload.get("hook") or ""),
            body=body,
            outro=str(payload.get("outro") or ""),
            word_count=int(payload.get("word_count") or len(body)),
        )

    # 纯文本稿（真 LLM / mock 都走这条路）：首行 hook，末行 outro，中间是主体
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    hook = lines[0] if lines else ""
    outro = lines[-1] if len(lines) > 1 else ""
    body = "\n".join(lines[1:-1]) if len(lines) > 2 else (content.strip() or hook)
    return RewriteOutput(hook=hook, body=body, outro=outro, word_count=len(body))


# CP3.5-pre-4：装饰器只做 Langfuse 上报（trace_id / article_id / user_id），
# LANGFUSE_ENABLED=false（默认）时薄壳一层，step 逻辑不变。
@trace_distill_step("step1_structure")
async def step1_structure(ctx: DistillContext, llm) -> None:
    """Step 1: 内容结构化（Qwen2.5-VL，v1 §5.2.2）。"""
    prompt = STEP1_USER.format(title=ctx.title or "(无标题)", raw_content=ctx.raw_content)
    resp = await llm.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="system", content=STEP1_SYSTEM),
                ChatMessage(role="user", content=prompt),
            ],
            metadata={"task_id": ctx.task_id, "step": "step1_structure"},
        )
    )
    ctx.structured = _parse_structured(resp.content)


@trace_distill_step("step2_rewrite")
async def step2_rewrite(ctx: DistillContext, llm) -> None:
    """Step 2: 听感改写（Claude 4 Sonnet，v1 §5.2.3）。"""
    if ctx.structured is None:
        raise ValueError("structured not set, call step1 first")

    prompt = STEP2_USER.format(structured_json=ctx.structured.model_dump_json(indent=2))
    resp = await llm.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="system", content=STEP2_SYSTEM),
                ChatMessage(role="user", content=prompt),
            ],
            metadata={"task_id": ctx.task_id, "step": "step2_rewrite"},
        )
    )
    ctx.rewrite = _parse_rewrite(resp.content)


@trace_distill_step("step3_tts")
async def step3_tts(ctx: DistillContext, tts_client) -> None:
    """Step 3: TTS 合成（豆包 TTS，v1 §5.2.4）。"""
    if ctx.rewrite is None:
        raise ValueError("rewrite not set, call step2 first")

    prompt = STEP3_USER.format(rewrite_body=ctx.rewrite.body)
    segments = await tts_client.synthesize(prompt)
    ctx.tts = TTSOutput(segments=list(segments))


@trace_distill_step("step4_concat")
async def step4_concat(ctx: DistillContext) -> None:
    """Step 4: 音频拼接（FFmpeg，v1 §5.2.5）。"""
    if ctx.tts is None:
        raise ValueError("tts not set, call step3 first")

    # 本期 mock：直接拼 URL，不做真实 FFmpeg 拼接（CP3.5）
    ctx.final = AudioConcatOutput(
        audio_url=f"https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com/{ctx.article_id}.m4a",
        duration_sec=1800,  # 30 min
        format="m4a",
    )
