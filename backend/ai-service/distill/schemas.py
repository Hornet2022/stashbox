"""蒸馏中间结果 Pydantic schema（CP3.5-pre-2 + CP-DISTILL-QUALITY）。"""

from pydantic import BaseModel, Field


class StructuredChapter(BaseModel):
    """Step 1 输出的一章（CP-DISTILL-QUALITY：扩 quotes/tension，给 Step2 骨架）。"""

    title: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)  # CP-DQ：原文金句，下游必须原封引用
    tension: str = ""  # CP-DQ：本章情绪关键词


class StructuredOutput(BaseModel):
    """Step 1 输出：结构化内容。"""

    summary: str
    chapters: list[StructuredChapter]
    entities: list[str] = Field(default_factory=list)  # 关键实体
    tags: list[str] = Field(default_factory=list)  # 自动标签


class RewriteOutput(BaseModel):
    """Step 2 输出：听感改写稿（CP-DISTILL-QUALITY：sections 列表替代单 body）。"""

    hook: str = ""  # 开场钩子（≤ 80 字）
    # CP-DISTILL-QUALITY：sections 是有序节拍段（每段 80-180 字），替代原本的单一 body 字符串。
    # Step3 按 sections 切 TTS，Step4 在 sections 之间插静音 → 听感节奏。
    sections: list[str] = Field(default_factory=list)
    outro: str = ""  # 收束（≤ 80 字）
    word_count: int = 0

    @property
    def body(self) -> str:
        """CP-DQ 兼容：把 sections 用空行 join 成一个 body 字符串。

        Step3 默认按 sections 切分；下游若需要单一字符串（text_to_speak 等），
        走这个 property —— 行为等价于"原先的单 body 字段"。
        """
        return "\n\n".join(self.sections)


class TTSOutput(BaseModel):
    """Step 3 输出：TTS 音频段。"""

    segments: list[dict]  # [{"text": ..., "voice": ..., "audio_url": ...}, ...]


class AudioConcatOutput(BaseModel):
    """Step 4 输出：拼接后音频。

    `audio_url`：最终可播放 URL（可能是本地临时路径、占位 OSS URL、或真 OSS URL）
    `audio_bytes`：拼接后的 raw bytes，仅在真实 ffmpeg 路径下填充；用于 _save_audio 上传。
    `duration_sec`：音频时长（秒）
    `format`：音频格式（默认 m4a）
    """

    audio_url: str
    duration_sec: int
    format: str = "m4a"
    audio_bytes: bytes | None = None  # CP9.x：避免 _save_audio 重复 TTS


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
    # CP-DQ-VERIFY：step3 按节拍切分的待合成片段（观测 / 重试用）。
    # 长度 == 最终 tts.segments（每段对应一次 synthesize 调用）。
    # 与 script_text 的关系：sections 内部按"句末标点"切分后拼回去 == sections 原文（按字一字不差）。
    tts_chunks: list[str] = Field(default_factory=list)
