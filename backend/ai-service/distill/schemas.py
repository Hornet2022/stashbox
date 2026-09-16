"""蒸馏中间结果 Pydantic schema（CP3.5-pre-2）。"""
from pydantic import BaseModel, Field


class StructuredChapter(BaseModel):
    """Step 1 输出的一章。"""

    title: str
    summary: str
    key_points: list[str] = Field(default_factory=list)


class StructuredOutput(BaseModel):
    """Step 1 输出：结构化内容。"""

    summary: str
    chapters: list[StructuredChapter]
    entities: list[str] = Field(default_factory=list)  # 关键实体
    tags: list[str] = Field(default_factory=list)  # 自动标签


class RewriteOutput(BaseModel):
    """Step 2 输出：听感改写稿。"""

    hook: str  # 开场钩子（吸引注意力）
    body: str  # 主体改写（口语化）
    outro: str  # 结尾钩子
    word_count: int = 0


class TTSOutput(BaseModel):
    """Step 3 输出：TTS 音频段。"""

    segments: list[dict]  # [{"text": ..., "voice": ..., "audio_url": ...}, ...]


class AudioConcatOutput(BaseModel):
    """Step 4 输出：拼接后音频。"""

    audio_url: str
    duration_sec: int
    format: str = "m4a"


class DistillContext(BaseModel):
    """蒸馏任务上下文（贯穿 4 步）。"""

    task_id: str
    article_id: str
    user_id: int
    url: str
    raw_content: str  # 抓取的原文
    title: str | None = None

    # 中间结果
    structured: StructuredOutput | None = None
    rewrite: RewriteOutput | None = None
    tts: TTSOutput | None = None
    final: AudioConcatOutput | None = None
