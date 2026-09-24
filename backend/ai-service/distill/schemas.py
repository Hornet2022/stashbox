"""蒸馏中间结果 Pydantic schema（CP3.5-pre-2 + CP-DISTILL-QUALITY + CP3.7.1 + CP3.7.2）。"""

from datetime import datetime
from typing import Literal

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

    # === CP3.7.2 新增：hook 框架字段 ===
    # §2.2.A：DistillContext 扩字段
    user_profile: "UserListeningPattern | None" = None
    few_shot_examples: list["RewriteExample"] = Field(default_factory=list)
    target_tier: Literal["simple", "full"] = "full"
    target_bitrate: list[int] = Field(default_factory=lambda: [128])
    evaluation_id: str | None = None
    # CP3.6.4 stage cache（stage 重试时复用）
    stage_cache: dict[str, dict] = Field(default_factory=dict)
    # === CP5.6.0 新增：个性化 + A/B 测试标记 ===
    is_personalized: bool = False  # CP5.6.0 §2.7 观测
    ab_group: Literal["personalized", "general"] | None = None  # CP5.6.0 §2.7 D


class RewriteExample(BaseModel):
    """CP3.7.2 §2.2.A：few-shot 单条样本（Step 2 prompt 注入用）。"""

    kind: Literal["hook", "section", "outro"]
    text: str
    score_avg: float


# ---------------------------------------------------------------------------
# CP3.7.1 §2.1：听感产品化数据底座（4 张表的 Pydantic view）
# ORM 在 common/models/ 下，Pydantic 在 distill/schemas.py（业务层用）
# ---------------------------------------------------------------------------
class DistillationEvaluation(BaseModel):
    """CP3.7.1 §2.1.A：听感评分（4 维 + 总分）。

    overall_score <= 2 → 触发自动重蒸（CP3.7.3）。
    """

    id: str
    task_id: str
    user_id: int
    hook_score: int | None = None
    section_score: int | None = None
    outro_score: int | None = None
    rhythm_score: int | None = None
    overall_score: int
    comment: str | None = None
    skip_reason: str | None = None
    auto_flag: bool = False
    retried_task_id: str | None = None
    created_at: datetime


class UserListeningPattern(BaseModel):
    """CP3.7.1 §2.1.B：用户听感画像（个性化改写的输入）。"""

    user_id: int
    feedback_count: int = 0
    avg_session_sec: int | None = None
    skip_rate: float | None = None
    completion_rate: float | None = None
    preferred_rhythm: str | None = None
    preferred_hook_type: str | None = None
    avg_overall_score: float | None = None
    last_distill_at: str | None = None
    last_updated: datetime


class ArticleAudioVariant(BaseModel):
    """CP3.7.1 §2.1.C：音频多码率变体（闭环 3 数据底座）。"""

    id: str
    distilled_article_id: str
    bitrate: int  # 64 / 96 / 128 kbps
    file_size_bytes: int
    oss_key: str
    format: str = "m4a"
    duration_sec: int
    sample_rate: int = 24000
    mono: bool = True
    created_at: datetime


class FewShotExample(BaseModel):
    """CP3.7.1 §2.1.D：few-shot 池条目（高分改写片段库）。"""

    id: str
    user_id: int | None = None  # NULL = 全局池
    source_pattern: str
    rewrite_text: str
    kind: str  # 'hook' / 'section' / 'outro'
    score_avg: float
    source_eval_ids: str  # JSON array 字符串
    usage_count: int = 0
    last_used_at: datetime | None = None
    active: bool = True
    created_at: datetime


class ConsentRecord(BaseModel):
    """CP5.6.0 §3.1：用户同意记录（个性化 + 跨用户金句 + 隐私政策 v2）。"""

    user_id: int
    personalization_enabled: bool = False  # opt-in
    cross_user_share_enabled: bool = False
    consent_at: str | None = None
    consent_version: str = "v2"


class ColdStartState(BaseModel):
    """CP5.6.1 §3.1 决策 2：冷启动状态。"""

    feedback_count: int
    state: Literal["fresh", "warming", "active"]
    ratings_remaining_to_personalize: int
    personalization_enabled_at: datetime | None = None


class PoolHealthReport(BaseModel):
    """CP5.7.0 §3.1 决策 4：few-shot 池健康度报告。"""

    total_count: int
    high_score_count: int
    medium_score_count: int
    low_score_count: int
    active_count: int
    stale_count: int
    health_score: float
    warning: str | None = None
