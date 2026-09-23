"""蒸馏 4 步函数（CP3.5-pre-2，v1 §5.2.2 - §5.2.5）。

每步接 LLMClient（Step 3 接 TTS client），输出中间结果写到 context。
LLM 响应按「JSON 优先、纯文本兜底」解析 —— mock client 与真 LLM 都能跑通。
"""

import io
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import structlog

from llm.types import ChatMessage, ChatRequest
from observability.decorators import trace_distill_step

from .prompts import STEP1_SYSTEM, STEP1_USER, STEP2_SYSTEM, STEP2_USER

log = structlog.get_logger(__name__)
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
# CP-MOCK-SENTINEL：mock 兜底统一单值，pipeline._write_final_to_db 写入前会过滤。
# 之前 ["mock-tag-1","mock-tag-2"] 写库后污染订阅推送链路。
_FALLBACK_TAGS = ["__mock__"]


def _load_json(content: str) -> dict | list | None:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return None


def _coerce_chapter(raw) -> StructuredChapter:
    """章节允许 dict（真 LLM）或 str（mock client 的 `chapters: ["intro", ...]`）。

    CP-DISTILL-QUALITY：扩 quotes / tension 字段，Step2 写作时的金句库 + 节奏线。
    """
    if isinstance(raw, dict):
        return StructuredChapter(
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            key_points=[str(p) for p in raw.get("key_points") or []],
            quotes=[str(q) for q in raw.get("quotes") or []],
            tension=str(raw.get("tension") or ""),
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
    """CP-DISTILL-QUALITY：sections 列表替代单 body 字段。

    优先 JSON 解析；JSON 失败回退到纯文本（首行 hook，末行 outro，中间按空行分段）。
    """
    payload = _load_json(content)
    if isinstance(payload, dict):
        hook = str(payload.get("hook") or "")
        outro = str(payload.get("outro") or "")

        # sections 列表：CP-DQ 主路径
        raw_sections = payload.get("sections")
        if isinstance(raw_sections, list) and raw_sections:
            sections = [str(s).strip() for s in raw_sections if str(s or "").strip()]
        elif "body" in payload:
            # 兼容旧版单 body 字段：按空行拆成 sections
            body_str = str(payload.get("body") or "").strip()
            sections = [s.strip() for s in body_str.split("\n\n") if s.strip()] or (
                [body_str] if body_str else []
            )
        else:
            sections = []

        wc = int(payload.get("word_count") or 0)
        if not wc:
            wc = sum(len(s) for s in sections) + len(hook) + len(outro)
        return RewriteOutput(hook=hook, sections=sections, outro=outro, word_count=wc)

    # 纯文本兜底（真 LLM 偶尔也走这条路）：首行 hook，末行 outro，中间按空行分段
    parts = [p for p in content.split("\n\n") if p.strip()]
    if len(parts) >= 2:
        hook = parts[0].strip()
        outro = parts[-1].strip()
        sections = [p.strip() for p in parts[1:-1] if p.strip()]
    else:
        # 全文当 body
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        hook = lines[0] if lines else ""
        outro = lines[-1] if len(lines) > 1 else ""
        body = "\n".join(lines[1:-1]) if len(lines) > 2 else (content.strip() or hook)
        sections = [body] if body else []
    wc = len(hook) + sum(len(s) for s in sections) + len(outro)
    return RewriteOutput(hook=hook, sections=sections, outro=outro, word_count=wc)


# CP3.5-pre-4：装饰器只做 Langfuse 上报（trace_id / article_id / user_id），
# LANGFUSE_ENABLED=false（默认）时薄壳一层，step 逻辑不变。
@trace_distill_step("step1_structure")
async def step1_structure(ctx: DistillContext, llm) -> None:
    """Step 1: 内容结构化（Qwen2.5-VL，v1 §5.2.2）。

    CP-TAG-FILTER：从 tags 表拉一次 tag.name 词汇注入 system prompt，
    让 LLM 严格从系统标签里选——避免 LLM 输出漂移（如"按钮"/"科技资讯"）
    导致订阅推送漏发。失败时退化到无 vocabulary 提示（不破主流程）。
    """
    tag_vocab = await _load_tag_vocabulary()
    system_content = STEP1_SYSTEM.format(tag_vocabulary=tag_vocab)
    prompt = STEP1_USER.format(title=ctx.title or "(无标题)", raw_content=ctx.raw_content)
    resp = await llm.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="system", content=system_content),
                ChatMessage(role="user", content=prompt),
            ],
            metadata={"task_id": ctx.task_id, "step": "step1_structure"},
        )
    )
    ctx.structured = _parse_structured(resp.content)


async def _load_tag_vocabulary() -> str:
    """拉一次 tags.name 列表（CP-TAG-FILTER + CP3.6.2 Redis 缓存）。

    CP3.6.2：走 `tag_vocab_cache.load_tag_vocabulary()` —— 先查 Redis (5min TTL)，
    miss 查 DB → 写回 Redis。失败兜底同原行为。
    """
    from stashbox.backend.common.database import AsyncSessionLocal
    from .tag_vocab_cache import load_tag_vocabulary as _load

    try:
        async with AsyncSessionLocal() as db:
            return await _load(db)
    except Exception as exc:
        # 兜底文案（与 CP3.6.2 前完全一致）
        log.warning("tag_vocabulary_load_failed", error=str(exc))
        return "（候选标签暂不可用，请自由输出 3-5 个主题词）"


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


# CP-DISTILL-PUNCT：Step2 prompt 里允许的停顿占位标记（regex 提取）。
# Step3 合成前 strip 掉，让 TTS 看到的是干净文本；Step4 会根据段边界插入静音。
_PUNCT_TOKEN_RE = re.compile(r"<#(\d+(?:\.\d+)?)#>")
# 句末标点：优先在这些位置断开，保证语义完整
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")
# 单段最大字符数：实测 IndexTTS 安全阈（超过 100 字符吐字时长非线性）。
TTS_CHUNK_MAX_CHARS = 100


def _strip_punct_tokens(text: str) -> str:
    """删掉 Step2 留下的 `<#0.6#>` 停顿标记，合并相邻空白。

    CP-DQ-READ-ALOUD：把 IndexTTS/豆包容易读错或不会读的字符改成 TTS 友好形式：
    - 全角标点 → 半角（让 TTS 标点停顿更稳）
    - 阿拉伯数字保留原文（TTS 会按中文读法"三十"等），但 `M月D日` 补全单位
    - 百分号 % → 写成"百分之"（多数 TTS 会读成英文"percent"）
    - 英文缩写（CEO/CTO/AI/AGI 等）保留原文，但加 `·` 分隔防止字符吞字
    - 反复问号"？！"、"？！" → 单个（避免 TTS 跳读）
    """
    s = _PUNCT_TOKEN_RE.sub("", text or "")
    if not s:
        return ""
    # 1. 反复出现的句末标点（？！）+（？！）→ 单个问号/感叹号
    s = re.sub(r"([？！!?]){2,}", r"\1", s)
    # 2. 百分号 → "百分之"（让 TTS 用中文读法）
    s = re.sub(r"(\d+(\.\d+)?)\s*%", r"\1百分之", s)
    # 3. 全角括号 → 半角（部分 TTS 不会读全角）
    s = s.replace("（", "(").replace("）", ")")
    # 4. 合并连续空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _plan_segments(rewrite) -> list[str]:
    """CP-DQ：按 sections 顺序产 TTS 段。

    每段 = 一个 sections 单元；若单段超过 100 字符，再走 `_chunk_for_tts()` 二次切。
    返回的列表就是 Step3 喂给 TTS 的输入顺序；Step4 按 list 顺序拼接，
    自然在 sections 之间产生静音（待 Step4 实现段间停顿）。
    """
    parts: list[str] = []
    for sec in rewrite.sections or []:
        clean = _strip_punct_tokens(sec)
        if not clean:
            continue
        if len(clean) <= TTS_CHUNK_MAX_CHARS:
            parts.append(clean)
        else:
            parts.extend(_chunk_for_tts(clean))
    return parts


def _chunk_for_tts(text: str, max_chars: int = TTS_CHUNK_MAX_CHARS) -> list[str]:
    """把长文本按句切成 ≤max_chars 的片段，供逐段 TTS。

    策略：
    1. 先按句末标点切句
    2. 贪心合并相邻句，累计不超过 max_chars；单句超长则按逗号再切，仍超长则硬切
    返回非空片段列表（全空则返回空列表，调用方兜底）。
    """
    text = (text or "").strip()
    if not text:
        return []
    # 拆句（保留标点）
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s and s.strip()]
    # 单句超长 → 按次级标点/长度再切
    pieces: list[str] = []
    for s in sentences:
        s = s.strip()
        if len(s) <= max_chars:
            pieces.append(s)
            continue
        # 先按逗号/顿号/空格切
        sub = re.split(r"(?<=[，,、\s])", s)
        buf = ""
        for t in sub:
            if len(buf) + len(t) <= max_chars:
                buf += t
            else:
                if buf:
                    pieces.append(buf.strip())
                # t 本身仍超长 → 硬切
                while len(t) > max_chars:
                    pieces.append(t[:max_chars])
                    t = t[max_chars:]
                buf = t
        if buf.strip():
            pieces.append(buf.strip())
    # 贪心合并小片段，减少请求数
    merged: list[str] = []
    cur = ""
    for p in pieces:
        if not cur:
            cur = p
        elif len(cur) + len(p) <= max_chars:
            cur += p
        else:
            merged.append(cur)
            cur = p
    if cur:
        merged.append(cur)
    return [m for m in merged if m.strip()]


@trace_distill_step("step3_tts")
async def step3_tts(ctx: DistillContext, tts_client) -> None:
    """Step 3: TTS 合成（豆包/IndexTTS，v1 §5.2.4 + CP-DISTILL-QUALITY）。

    CP9.x：直接喂文本，不套 STEP3_USER prompt（模板元指令会被念出来）。

    CP-DISTILL-LEN：按句分片，逐段合成。IndexTTS / 部分豆包音色对超长输入
    会静默截断（实测 240 字只吐 ~13s）。

    CP-DISTILL-QUALITY（新增）：按 **sections 顺序** 切分 TTS。
    - hook / 每个 section / outro 各 1+ 段；
    - 段内再做 ≤100 字符安全切；
    - `<#0.3#>` / `<#0.6#>` / `<#1.0#>` 停顿标记在送 TTS 前 strip 掉；
    - step4 按 sections 边界在段间插静音（节奏感来源）。

    兼容两种 client：
      - 返回 bytes（豆包/IndexTTS 真实音频）：每片包成 {"bytes": ...}。
      - 返回 list[dict]（MockTTSClient 占位段）：整段原样透传（不分片）。
    """
    if ctx.rewrite is None:
        raise ValueError("rewrite not set, call step2 first")

    # CP-DQ：把 hook + sections + outro 按节拍顺序拼成段列表，
    # 并**记录每个 segment 的"停顿属性"**（首/末/段间），让 step4 按此插静音。
    # sections 为空时退化为用 hook+outro 拼一个段（旧契约兼容）。
    chunks: list[str] = []
    gap_after_ms: list[int] = []  # 与 chunks 同长；每段末尾的停顿毫秒

    # CP-DQ-AUTO-PUNCT：每个 section 内部按"句末标点"切 TTS 段。
    # TTS 段 = 一句或几个短句 ≤ 100 字。每个 TTS 段都是语义完整的句子，
    # 合成完成后 step4 会根据段尾标点的"情感重量"加不同长度的静音帧，
    # 整体听感像"主播一句一停"，而不是"念一坨"。
    def _split_section_into_tts_segments(text: str) -> list[str]:
        """把一个 section 切成 TTS 段列表。

        策略：
        1. 先按句末标点（。！？；\n）切句
        2. 单句 < 100 字 → 直接作为段
        3. 单句 > 100 字 → 走 _chunk_for_tts 二次切
        4. 多个连续短句（< 30 字）→ 贪心合并成一个段，减少 TTS 次数
        """
        sents = [s for s in _SENT_SPLIT_RE.split(text or "") if s and s.strip()]
        if not sents:
            return []
        # 先做长度上限切
        pieces: list[str] = []
        for s in sents:
            s = s.strip()
            if len(s) <= TTS_CHUNK_MAX_CHARS:
                pieces.append(s)
            else:
                pieces.extend(_chunk_for_tts(s))
        # 短句合并：相邻短句（< 30 字）贪心合一段
        merged: list[str] = []
        cur = ""
        for p in pieces:
            if not cur:
                cur = p
            elif len(cur) < 30 and len(cur) + len(p) <= TTS_CHUNK_MAX_CHARS:
                cur += p
            else:
                merged.append(cur)
                cur = p
        if cur:
            merged.append(cur)
        return [m.strip() for m in merged if m.strip()]

    def _gap_after_for(text: str, *, position: str) -> int:
        """根据段尾标点和段位置决定静音毫秒。

        position ∈ {"hook", "intra_section", "inter_section", "tooutro", "outro"}
        - hook → inter_section（700ms）：让钩子和正文之间有明显呼吸
        - intra_section（section 内部句子之间）：350ms
        - inter_section（节拍之间）：550ms
        - tooutro（最后一节最后一句 → outro）：700ms
        - outro 末尾：0（让 step4 用统一的 tail_gap_ms 收尾）
        """
        if position == "outro":
            return 0
        # 检测结尾标点
        tail = (text or "").rstrip()[-1:] if text else ""
        # 问号/感叹号 → 长停（强调）
        if tail in ("？", "?", "！", "!"):
            base = 500
        elif tail in ("。", ".", ";", "；"):
            base = 350
        elif tail in ("，", ",", "、"):
            # 段尾不应该停在逗号上，但 LLM 偶尔会。
            # 视为语气未完，短停让 TTS 自然续上
            base = 200
        else:
            base = 300
        if position == "hook":
            return max(base, 700)  # 钩子尾 → 第一节正文：至少 700ms
        if position == "inter_section":
            return max(base, 550)  # 节拍间：至少 550ms
        if position == "tooutro":
            return max(base, 700)  # 最后一节 → outro：至少 700ms
        # intra_section：基于标点的基础值
        return base

    if ctx.rewrite.sections:
        hook = _strip_punct_tokens(ctx.rewrite.hook)
        if hook:
            chunks.append(hook)
            gap_after_ms.append(_gap_after_for(hook, position="hook"))
        # 收集 sections 的 TTS 段，同时计算每段末尾的停顿
        sec_texts = [_strip_punct_tokens(s) for s in ctx.rewrite.sections]
        for s_i, st in enumerate(sec_texts):
            if not st:
                continue
            sub_segs = _split_section_into_tts_segments(st)
            if not sub_segs:
                continue
            is_last_sec = s_i == len(sec_texts) - 1
            for j, sb in enumerate(sub_segs):
                chunks.append(sb)
                is_last_sub = j == len(sub_segs) - 1
                if is_last_sub:
                    position = "tooutro" if is_last_sec else "inter_section"
                else:
                    position = "intra_section"
                gap_after_ms.append(_gap_after_for(sb, position=position))
        outro = _strip_punct_tokens(ctx.rewrite.outro)
        if outro:
            chunks.append(outro)
            gap_after_ms.append(_gap_after_for(outro, position="outro"))
    else:
        # 兼容旧 LLM 返回单 body 的情况：句间 350ms
        full_text = (ctx.rewrite.body or "").strip() or ctx.rewrite.hook
        chunks = _chunk_for_tts(full_text)
        gap_after_ms = [350] * (len(chunks) - 1) + [0]

    chunks = [c for c in chunks if c]
    # 对齐 gap_after_ms 与 chunks（chunk 数可能因 strip 后变少）
    gap_after_ms = gap_after_ms[: len(chunks)] + [0] * max(0, len(chunks) - len(gap_after_ms))
    if not chunks:
        raise ValueError("rewrite 内容为空，无法合成语音")
    ctx.tts_chunks = chunks  # 供观测/重试
    log.info(
        "tts_chunking",
        article_id=ctx.article_id,
        total_chars=sum(len(c) for c in chunks),
        chunks=len(chunks),
        sections=len(ctx.rewrite.sections),
        avg_gap_ms=int(sum(gap_after_ms) / max(1, len(gap_after_ms))),
        # CP-DQ-VERIFY：把每个 chunk 的前 60 字打到日志，便于"音频↔文本不一致"排错
        chunk_previews=[c[:60] for c in chunks],
    )

    segments: list[dict] = []
    for i, ch in enumerate(chunks):
        result = await tts_client.synthesize(ch)
        if isinstance(result, (bytes, bytearray)):
            segments.append({"bytes": bytes(result), "index": i, "gap_after_ms": gap_after_ms[i]})
        elif isinstance(result, list):
            # Mock 路径：client 自己返回段列表，整体并入（gap_after_ms 默认 0）
            segments.extend(result)
        else:
            raise TypeError(f"tts_client.synthesize() 返回类型不受支持: {type(result).__name__}")

    ctx.tts = TTSOutput(segments=segments)


def _find_bin(name: str) -> str | None:
    """在常见位置找 ffmpeg/ffprobe（PATH 里常没有 homebrew 的）。"""
    for cand in (
        shutil.which(name),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ):
        if cand and Path(cand).exists():
            return cand
    return None


def _probe_duration(path: Path) -> float | None:
    """ffprobe 取真实时长；无 ffprobe / 失败返回 None（调用方退化估算）。"""
    ffprobe = _find_bin("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except (FileNotFoundError, ValueError):
        pass
    return None


def _wav_duration_bytes(data: bytes) -> float | None:
    """从 WAV bytes 头部直接解析时长（帧数/采样率），不依赖 ffprobe。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            fr = w.getframerate()
            if fr <= 0:
                return None
            return w.getnframes() / fr
    except Exception:
        return None


def _concat_wavs(list_data: list[bytes]) -> bytes | None:
    """把多个 WAV bytes 用纯 Python 拼成一段（IndexTTS 统一 24k/mono/16bit）。

    任一段非 WAV，或参数（采样率/声道/位深）不一致 → 返回 None，交给调用方
    回退 ffmpeg。拼接保留第一个 WAV 参数，串接各段 PCM 帧。
    这是 CP-DISTILL-LEN 分片后 step4 的主路径（homebrew ffmpeg 不在 PATH，
    子进程裸调 `ffmpeg` 会 FileNotFoundError）。
    """
    if not list_data:
        return None
    params: tuple | None = None
    pcm_parts: list[bytes] = []
    for d in list_data:
        try:
            with wave.open(io.BytesIO(d), "rb") as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames = w.readframes(w.getnframes())
        except Exception:
            return None
        if params is None:
            params = p
        elif p != params:
            return None
        pcm_parts.append(frames)
    if params is None:
        return None
    nch, sw, fr = params
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        w.writeframes(b"".join(pcm_parts))
    return out.getvalue()


def _concat_wavs_with_gaps(
    list_data: list[bytes],
    inter_gap_ms: int = 350,
    lead_gap_ms: int = 200,
    tail_gap_ms: int = 500,
    silence_db: float = -40.0,
) -> bytes | None:
    """CP-DQ：把多个 WAV 段拼接，并在**段间**插 inter_gap_ms 静音，
    整体首尾各留 lead/tail_gap_ms 静音（让整条音频有"呼吸感"）。
    静音帧直接生成对应采样数的 0 值 PCM 数据——不引入噪声，听起来干净。
    """
    if not list_data:
        return None
    params: tuple | None = None
    pcm_parts: list[bytes] = []
    for d in list_data:
        try:
            with wave.open(io.BytesIO(d), "rb") as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames = w.readframes(w.getnframes())
        except Exception:
            return None
        if params is None:
            params = p
        elif p != params:
            return None
        pcm_parts.append(frames)
    if params is None:
        return None
    nch, sw, fr = params
    # silence_db 不做真实 dB 计算（要简单）：用半幅静默值模拟底噪。
    # 16-bit PCM 半幅 = 0x0001，落在 -96dB 远端，听感上是干净静音。
    silence_byte = 0x00
    if sw == 2:
        # 16-bit：用极小幅值（1 LSB）模拟"环境底噪"，避免纯数字 0 在某些解码器上爆裂
        silence_byte = 1  # little endian 16-bit：低字节 0x01 高字节 0x00 = 0x0001
    silence_frame = bytes([silence_byte] * (nch * sw))

    def gap(ms: int) -> bytes:
        return silence_frame * max(1, int(round(fr * ms / 1000)))

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        if lead_gap_ms > 0:
            w.writeframes(gap(lead_gap_ms))
        for i, pcm in enumerate(pcm_parts):
            w.writeframes(pcm)
            if i < len(pcm_parts) - 1 and inter_gap_ms > 0:
                w.writeframes(gap(inter_gap_ms))
        if tail_gap_ms > 0:
            w.writeframes(gap(tail_gap_ms))
    return out.getvalue()


def _concat_wavs_with_per_segment_gaps(
    list_data: list[bytes],
    gaps_ms: list[int],
    lead_gap_ms: int = 150,
    tail_gap_ms: int = 600,
) -> bytes | None:
    """CP-DQ：每个段独立指定末尾停顿毫秒（来自 step3 的 gap_after_ms 标记）。

    与 `_concat_wavs_with_gaps` 的区别：
    - 段间停顿因段而异（hook 后 700ms、节间 500ms、段内子段 200ms、末段 0）
    - 头部留 lead_gap_ms（让开头不抢耳）
    - 末段之后留 tail_gap_ms（整体收尾的呼吸感）
    """
    if not list_data or not gaps_ms or len(list_data) != len(gaps_ms):
        return None
    params: tuple | None = None
    pcm_parts: list[bytes] = []
    for d in list_data:
        try:
            with wave.open(io.BytesIO(d), "rb") as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames = w.readframes(w.getnframes())
        except Exception:
            return None
        if params is None:
            params = p
        elif p != params:
            return None
        pcm_parts.append(frames)
    if params is None:
        return None
    nch, sw, fr = params
    silence_byte = 1 if sw == 2 else 0  # 16-bit 用 1 LSB；其他位深用 0
    silence_frame = bytes([silence_byte] * (nch * sw))

    def gap(ms: int) -> bytes:
        return silence_frame * max(1, int(round(fr * ms / 1000)))

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        if lead_gap_ms > 0:
            w.writeframes(gap(lead_gap_ms))
        last = len(pcm_parts) - 1
        for i, pcm in enumerate(pcm_parts):
            w.writeframes(pcm)
            g = gaps_ms[i]
            if g > 0:
                w.writeframes(gap(g))
            elif i == last and tail_gap_ms > 0:
                # 末段 gap_after_ms=0 时，落 tail_gap_ms 收尾
                w.writeframes(gap(tail_gap_ms))
    return out.getvalue()


@trace_distill_step("step4_concat")
async def step4_concat(ctx: DistillContext) -> None:
    """Step 4: 音频拼接（v1 §5.2.5）。

    CP-DISTILL-LEN 修复：step3 分片后这里有多段音频，按优先级拼接：
      1. 纯 Python WAV concat（IndexTTS 主路径，统一 24k/mono/16bit；
         不依赖 ffmpeg —— homebrew 的 ffmpeg 不在 worker 进程 PATH 里，
         裸调 subprocess("ffmpeg") 会 FileNotFoundError）。
      2. ffmpeg concat（豆包 mp3 段等格式不一致场景的兜底）。
    duration_sec 一律取**真实时长**（WAV 头解析 / ffprobe），不再硬编码 30。

    Mock 路径（无 bytes）：audio_url 留空 + duration 0（保持 CP9.x 语义，
    distill_task 的 ready 判定依赖 audio_url 非空）。
    """
    if ctx.tts is None:
        raise ValueError("tts not set, call step3 first")

    segments = ctx.tts.segments or []
    real_bytes = [s["bytes"] for s in segments if isinstance(s, dict) and s.get("bytes")]

    if not real_bytes:
        # Mock 路径：CP9.x 修复 — audio_url 留空（不写假 URL），
        # 让 distill_task.py 的 ready 判定因 `da.audio_url` 为空而跳过，
        # 避免文章显示 ready 但实际没有可播放音频。
        ctx.final = AudioConcatOutput(
            audio_url="",  # 留空 = 未完成真实音频
            duration_sec=0,
            format="m4a",
        )
        return

    # -- 路径 1：单段直接落盘 -------------------------------------------
    if len(real_bytes) == 1:
        only = real_bytes[0]
        fmt = "wav" if only[:4] == b"RIFF" else "m4a"
        # 真实时长：WAV 头可直接解析；mp3 等尝试 ffprobe（写临时文件）
        duration = _wav_duration_bytes(only)
        if duration is None:
            tmp = Path(tempfile.gettempdir()) / f"stashbox_probe_{ctx.article_id}.{fmt}"
            try:
                tmp.write_bytes(only)
                duration = _probe_duration(tmp)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if not duration:
            raise RuntimeError(f"无法确定音频时长（fmt={fmt}, bytes={len(only)}）——拒绝写假时长")
        ctx.final = AudioConcatOutput(
            audio_url="",  # 让 _save_audio 决定真实 URL
            duration_sec=int(round(duration)),
            format=fmt,
            audio_bytes=only,
        )
        return

    # -- 路径 2：纯 Python WAV concat（多段，IndexTTS 主路径） -----------
    all_wav = all(b[:4] == b"RIFF" for b in real_bytes)
    if all_wav:
        # CP-DQ：每个 segment 携带 gap_after_ms；拼接时按此在段间插静音帧。
        # 兼容旧的没标 gap_after_ms 的段（默认 300ms 节间停顿）。
        per_seg_gaps = [
            int(s.get("gap_after_ms", 0)) if isinstance(s, dict) else 0
            for s in segments
            if isinstance(s, dict) and s.get("bytes")
        ]
        # 段间停顿时长：取每个段 gap_after_ms，末段 fallback tail_gap_ms=600
        joined = (
            _concat_wavs_with_gaps(
                real_bytes,
                inter_gap_ms=300,  # 默认（兼容旧 segments）
                lead_gap_ms=150,
                tail_gap_ms=600,
            )
            if not any(per_seg_gaps)
            else _concat_wavs_with_per_segment_gaps(
                real_bytes,
                per_seg_gaps,
                lead_gap_ms=150,
                tail_gap_ms=600,
            )
        )
        if joined is not None:
            duration = _wav_duration_bytes(joined) or 0
            if duration <= 0:
                raise RuntimeError("WAV 拼接后时长解析失败，拒绝写假时长")
            ctx.final = AudioConcatOutput(
                audio_url="",  # _save_audio 上传后决定真实 URL
                duration_sec=int(round(duration)),
                format="wav",
                audio_bytes=joined,
            )
            log.info(
                "tts_concat_wav_ok",
                article_id=ctx.article_id,
                segments=len(real_bytes),
                total_gap_ms=sum(per_seg_gaps),
                duration_sec=int(round(duration)),
            )
            return
        # 参数不一致（采样率/声道不同）→ 落 ffmpeg

    # -- 路径 3：ffmpeg concat 兜底（mp3/m4a 或混合格式） ----------------
    ffmpeg = _find_bin("ffmpeg")
    if not ffmpeg:
        # 无法拼接多段非 WAV：宁失败也不产出「只有第一段」的错误音频
        raise RuntimeError(f"多段 {len(real_bytes)}×非WAV 音频需要 ffmpeg 拼接，但未找到可执行文件")
    tmp = Path(tempfile.gettempdir()) / f"stashbox_concat_{ctx.article_id}"
    tmp.mkdir(parents=True, exist_ok=True)
    seg_files: list[Path] = []
    list_file = tmp / "list.txt"
    try:
        for i, b in enumerate(real_bytes):
            ext = "wav" if b[:4] == b"RIFF" else "mp3"
            p = tmp / f"seg_{i}.{ext}"
            p.write_bytes(b)
            seg_files.append(p)
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in seg_files), encoding="utf-8"
        )

        out_file = tmp / f"{ctx.article_id}.m4a"
        # 段为 mp3（豆包 TTS 默认输出），不能直接 -c copy 进 m4a(MP4) 容器
        # （ffmpeg 报 "Could not find tag for codec mp3 in stream"）。
        # 统一转码为 AAC，产出真正可播放的 .m4a。
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out_file),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat 失败: {proc.stderr[-500:]}")

        duration = _probe_duration(out_file)
        if not duration:
            raise RuntimeError("ffmpeg 产物时长解析失败，拒绝写假时长")
        # CP9.x：先把拼接后的 bytes 读进内存（_save_audio 直接上传，不需要再调一次 TTS）
        out_bytes = out_file.read_bytes() if out_file.exists() else b""
        ctx.final = AudioConcatOutput(
            # 本地临时文件；pipeline._save_audio 上传 OSS 后会覆盖为真实 URL
            audio_url=str(out_file),
            duration_sec=int(round(duration)),
            format="m4a",
            audio_bytes=out_bytes,
        )
    finally:
        for p in seg_files:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            list_file.unlink()
        except OSError:
            pass
