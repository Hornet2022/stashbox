"""蒸馏流水线编排（CP3.5-pre-2）。

编排 4 步 + 状态机推进 + 失败退还配额。
DB 写回在 session_factory 为 None 时跳过（单测 / 未接库场景）。
"""

import time

import structlog

from llm import LLMClient

from .schemas import AudioConcatOutput, DistillContext
from .state_machine import DistillStatus, transition
from .steps import step1_structure, step2_rewrite, step3_tts, step4_concat

log = structlog.get_logger("distill")

MOCK_QUALITY_SCORE = 8.5


def _get_tts_client():
    """懒加载 TTS client（避免顶层导入循环依赖）。"""
    from stashbox.backend.app.services.tts import get_tts_client

    return get_tts_client()


def _get_storage():
    """懒加载 storage client。"""
    from stashbox.backend.app.services.storage import get_storage

    return get_storage()


class DistillPipeline:
    """蒸馏流水线编排：4 步 + 状态机 + DB 写回。"""

    def __init__(self, llm: LLMClient, tts_client=None, db_session_factory=None):
        self.llm = llm
        self._tts_client = tts_client
        self.session_factory = db_session_factory
        self._current = DistillStatus.QUEUED
        self.status_history: list[DistillStatus] = [DistillStatus.QUEUED]

    @property
    def tts_client(self):
        return self._tts_client or _get_tts_client()

    async def run(self, ctx: DistillContext) -> DistillContext:
        """跑完整 4 步流水线。

        状态机推进 + 异常处理（任何一步失败 → failed + 退还配额）。
        """
        start = time.time()
        self._current = DistillStatus.QUEUED
        self.status_history = [DistillStatus.QUEUED]
        log.info("distill_started", task_id=ctx.task_id, article_id=ctx.article_id)

        try:
            # Step 1
            await self._update_status(ctx, DistillStatus.STEP1_STRUCTURING)
            await step1_structure(ctx, self.llm)

            # Step 2
            await self._update_status(ctx, DistillStatus.STEP2_REWRITING)
            await step2_rewrite(ctx, self.llm)

            # Step 3
            await self._update_status(ctx, DistillStatus.STEP3_TTSING)
            await step3_tts(ctx, self.tts_client)

            # Step 4
            await self._update_status(ctx, DistillStatus.STEP4_CONCATENATING)
            await step4_concat(ctx)

            # CP7.2: TTS 合成音频 → 存本地 → 更新 audio_url
            await self._save_audio(ctx)

            # Done
            await self._write_final_to_db(ctx)
            await self._update_status(ctx, DistillStatus.DONE)

            log.info(
                "distill_completed",
                task_id=ctx.task_id,
                duration_ms=(time.time() - start) * 1000,
            )
            return ctx
        except Exception as e:
            log.exception("distill_failed", task_id=ctx.task_id, error=str(e))
            await self._update_status(ctx, DistillStatus.FAILED)
            await self._refund_quota(ctx)  # 退还配额（CP1.6 已实现）
            raise

    async def _update_status(self, ctx: DistillContext, status: DistillStatus) -> None:
        """推进状态机（非法转换抛 ValueError）+ 更新 DB 状态。"""
        self._current = transition(self._current, status)
        self.status_history.append(status)

        if self.session_factory is None:
            return  # 单测可跳过
        from sqlalchemy import func, update

        from stashbox.backend.common.models import DistilledArticle

        async with self.session_factory() as session:
            await session.execute(
                update(DistilledArticle)
                .where(DistilledArticle.id == ctx.task_id)
                .values(status=status.value, updated_at=func.now())
            )
            await session.commit()

    async def _save_audio(self, ctx: DistillContext) -> None:
        """CP7.2: 用 rewrite.body 调 TTS → Storage → 更新 ctx.final.audio_url。"""
        if ctx.rewrite is None:
            return
        try:
            tts = self.tts_client
            storage = _get_storage()
            # 用 rewrite.body 做 TTS（口语化正文）
            audio_bytes = await tts.synthesize(ctx.rewrite.body)
            audio_key = f"audio/{ctx.article_id}.mp3"
            audio_url = await storage.save(audio_key, audio_bytes)
            # 更新 ctx.final（_write_final_to_db 会写这个值到 DB）
            if ctx.final is None:
                ctx.final = AudioConcatOutput(audio_url=audio_url, duration_sec=0, format="mp3")
            else:
                ctx.final.audio_url = audio_url
            log.info("audio_saved", article_id=ctx.article_id, audio_url=audio_url)
        except Exception as e:
            log.warning("audio_save_failed", article_id=ctx.article_id, error=str(e))
            # 不破主流程：audio_url 保持 step4 的 mock URL

    async def _write_final_to_db(self, ctx: DistillContext) -> None:
        """最终结果写 DB（audio_url + duration + tags + quality_score）。"""
        if self.session_factory is None or ctx.final is None:
            return
        from sqlalchemy import func, update

        from stashbox.backend.common.models import DistilledArticle

        async with self.session_factory() as session:
            await session.execute(
                update(DistilledArticle)
                .where(DistilledArticle.id == ctx.task_id)
                .values(
                    audio_url=ctx.final.audio_url,
                    duration_sec=ctx.final.duration_sec,
                    tags=ctx.structured.tags if ctx.structured else [],
                    quality_score=MOCK_QUALITY_SCORE,  # mock 评分
                    updated_at=func.now(),
                )
            )
            await session.commit()

    async def _refund_quota(self, ctx: DistillContext) -> None:
        """蒸馏失败退还配额（CP1.6 已实现）。"""
        if self.session_factory is None:
            return
        from stashbox.backend.common import quota_service

        async with self.session_factory() as session:
            await quota_service.refund(session, ctx.user_id)
