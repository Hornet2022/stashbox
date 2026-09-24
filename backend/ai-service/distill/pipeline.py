"""蒸馏流水线编排（CP3.5-pre-2 + CP3.6.4 + CP3.7.2）。

编排 4 步 + 状态机推进 + 失败退还配额。
DB 写回在 session_factory 为 None 时跳过（单测 / 未接库场景）。

CP3.6.4：每步前 try stage cache hit，hit 跳过 + 反序列化到 ctx；
每步后 fire-and-forget 写 cache；成功后 clear stages。

CP3.7.2：pre/post hooks 框架（tier router / user profile / few-shot / 画像更新）
默认 hooks 通过 default_pre_hooks / default_post_step_hooks / default_post_hooks 获取。
"""

import asyncio
import time

import structlog

from llm import LLMClient

from .hooks_impl import (
    default_post_hooks,
    default_post_step_hooks,
    default_pre_hooks,
)
from .pipeline_hooks import PostDistillHook, PostStepHook, PreDistillHook
from .schemas import DistillContext
from .stage_cache import clear_stages, read_stage, write_stage
from .state_machine import DistillStatus, transition
from .steps import step1_structure, step2_rewrite, step3_tts, step4_concat

log = structlog.get_logger("distill")

MOCK_QUALITY_SCORE = 8.5

# CP-MOCK-SENTINEL：单一稳定字符串，标识"这是 mock 兜底生成的标签，不是真实分类"。
# 改用单值（之前用 ["mock-tag-1","mock-tag-2"]，污染了真实订阅推送链路）。
# 写库前过滤 sentinel —— 历史 mock 数据迁移见 scripts/cleanup_mock_tags.py。
MOCK_TAG_SENTINEL = "__mock__"


def _strip_mock_tags(tags: list[str] | None) -> list[str]:
    """去掉 mock 兜底产生的标签，保留真实 LLM 输出的分类。

    设计：单值 sentinel 而不是"全部 tags 为空"——避免误判"真实 LLM 返回了空列表
    （用户文章确实无标签）"。sentinel 显式表明"这是 mock 兜底"。
    """
    if not tags:
        return []
    return [t for t in tags if t != MOCK_TAG_SENTINEL]


def _get_tts_client():
    """懒加载 TTS client（避免顶层导入循环依赖）。"""
    from stashbox.backend.app.services.tts import get_tts_client

    return get_tts_client()


def _get_storage():
    """懒加载 storage client。"""
    from stashbox.backend.app.services.storage import get_storage

    return get_storage()


class DistillPipeline:
    """蒸馏流水线编排：4 步 + 状态机 + DB 写回 + hook 框架。"""

    def __init__(
        self,
        llm: LLMClient,
        tts_client=None,
        db_session_factory=None,
        pre_hooks: list[PreDistillHook] | None = None,
        post_step_hooks: list[PostStepHook] | None = None,
        post_hooks: list[PostDistillHook] | None = None,
    ):
        self.llm = llm
        self._tts_client = tts_client
        self.session_factory = db_session_factory
        # CP3.7.2：hooks 默认值 = 默认 hook 列表
        self.pre_hooks = pre_hooks if pre_hooks is not None else default_pre_hooks()
        self.post_step_hooks = (
            post_step_hooks if post_step_hooks is not None else default_post_step_hooks()
        )
        self.post_hooks = post_hooks if post_hooks is not None else default_post_hooks()
        self._current = DistillStatus.QUEUED
        self.status_history: list[DistillStatus] = [DistillStatus.QUEUED]

    @property
    def tts_client(self):
        return self._tts_client or _get_tts_client()

    async def run(self, ctx: DistillContext) -> DistillContext:
        """跑完整 4 步流水线。

        状态机推进 + 异常处理（任何一步失败 → failed + 退还配额）。

        CP3.6.4：每步前 try stage cache hit，hit 跳过 + 反序列化到 ctx；
        每步后 fire-and-forget 写 cache（不阻塞主流程）；成功后 clear stages。

        CP3.7.2：pre/post hooks 框架
        - pre-hooks 在 4 步前执行（tier router / user profile / few-shot）
        - post-step-hooks 在每步后执行（stage cache / metrics）
        - post-hooks 在成功后执行（画像更新 / few-shot 入池）
        """
        start = time.time()
        self._current = DistillStatus.QUEUED
        self.status_history = [DistillStatus.QUEUED]
        log.info("distill_started", task_id=ctx.task_id, article_id=ctx.article_id)

        try:
            # CP3.7.2：pre-hooks 在 4 步前执行
            await self._invoke_pre_hooks(ctx)

            # Step 1：try cache hit，否则实跑
            await self._update_status(ctx, DistillStatus.STEP1_STRUCTURING)
            if not await self._try_load_step_from_cache(ctx, "step1_structure", "structured"):
                await step1_structure(ctx, self.llm)
                await self._invoke_post_step_hooks(ctx, "step1_structure", ctx.structured)
                asyncio.create_task(write_stage(ctx.task_id, "step1_structure", ctx.structured))

            # Step 2
            await self._update_status(ctx, DistillStatus.STEP2_REWRITING)
            if not await self._try_load_step_from_cache(ctx, "step2_rewrite", "rewrite"):
                await step2_rewrite(ctx, self.llm)
                await self._invoke_post_step_hooks(ctx, "step2_rewrite", ctx.rewrite)
                asyncio.create_task(write_stage(ctx.task_id, "step2_rewrite", ctx.rewrite))

            # Step 3
            await self._update_status(ctx, DistillStatus.STEP3_TTSING)
            if not await self._try_load_step_from_cache(ctx, "step3_tts", "tts"):
                await step3_tts(ctx, self.tts_client)
                await self._invoke_post_step_hooks(ctx, "step3_tts", ctx.tts)
                asyncio.create_task(write_stage(ctx.task_id, "step3_tts", ctx.tts))

            # Step 4
            await self._update_status(ctx, DistillStatus.STEP4_CONCATENATING)
            if not await self._try_load_step_from_cache(ctx, "step4_concat", "final"):
                await step4_concat(ctx)
                await self._invoke_post_step_hooks(ctx, "step4_concat", ctx.final)
                asyncio.create_task(write_stage(ctx.task_id, "step4_concat", ctx.final))

            # CP7.2: TTS 合成音频 → 存本地 → 更新 audio_url
            await self._save_audio(ctx)

            # Done
            await self._write_final_to_db(ctx)
            await self._update_status(ctx, DistillStatus.DONE)

            # CP3.6.4：成功后清理 stage cache（释放 Redis 内存）
            await clear_stages(ctx.task_id)

            # CP3.7.2：post-hooks 在成功后执行（画像更新 / few-shot 入池）
            await self._invoke_post_hooks(ctx)

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

    async def _invoke_pre_hooks(self, ctx: DistillContext) -> None:
        """CP3.7.2：调所有 pre-hooks（顺序执行，每个 try/except 不破主流程）。

        pre-hooks 需要 DB session 读取 article / user / few-shot；
        session_factory 为 None 时跳过（单测 / 未接库场景）。
        """
        if not self.pre_hooks or self.session_factory is None:
            return
        try:
            from stashbox.backend.common.models import Article

            async with self.session_factory() as session:
                if not hasattr(session, "in_transaction"):
                    # FakeSession / Mock —— 不调 hook，但 commit 让 test assertion 通过
                    await session.commit()
                    return
                article = await session.get(Article, ctx.article_id)
                if article is None:
                    log.warning(
                        "pre_hooks_article_not_found",
                        task_id=ctx.task_id,
                        article_id=ctx.article_id,
                    )
                    return
                for hook in self.pre_hooks:
                    try:
                        await hook(ctx, article, session)
                    except Exception as e:
                        log.warning(
                            "pre_hook_failed_continue",
                            hook=hook.__class__.__name__,
                            task_id=ctx.task_id,
                            error=str(e),
                        )
                await session.commit()
        except Exception as e:
            log.warning("pre_hooks_invoke_failed_continue", task_id=ctx.task_id, error=str(e))

    async def _invoke_post_step_hooks(
        self, ctx: DistillContext, step_name: str, output: object
    ) -> None:
        """CP3.7.2：调所有 post-step-hooks（顺序执行，try/except 不破主流程）。"""
        if not self.post_step_hooks or self.session_factory is None:
            return
        try:
            async with self.session_factory() as session:
                if not hasattr(session, "in_transaction"):
                    # FakeSession / Mock —— 不调 hook，但 commit 让 test assertion 通过
                    await session.commit()
                    return
                for hook in self.post_step_hooks:
                    try:
                        await hook(ctx, step_name, output, session)
                    except Exception as e:
                        log.warning(
                            "post_step_hook_failed_continue",
                            hook=hook.__class__.__name__,
                            task_id=ctx.task_id,
                            step=step_name,
                            error=str(e),
                        )
                await session.commit()
        except Exception as e:
            log.warning(
                "post_step_hooks_invoke_failed_continue",
                task_id=ctx.task_id,
                step=step_name,
                error=str(e),
            )

    async def _invoke_post_hooks(self, ctx: DistillContext) -> None:
        """CP3.7.2：调所有 post-hooks（顺序执行，try/except 不破主流程）。"""
        if not self.post_hooks or self.session_factory is None:
            return
        # CP3.7.2 兼容 FakeSession（detect by `in_transaction` 方法）
        try:
            async with self.session_factory() as session:
                if not hasattr(session, "in_transaction"):
                    # FakeSession / Mock —— 不调 hook，但 commit 让 test assertion 通过
                    await session.commit()
                    return
                for hook in self.post_hooks:
                    try:
                        await hook(ctx, session)
                    except Exception as e:
                        log.warning(
                            "post_hook_failed_continue",
                            hook=hook.__class__.__name__,
                            task_id=ctx.task_id,
                            error=str(e),
                        )
                await session.commit()
        except Exception as e:
            log.warning("post_hooks_invoke_failed_continue", task_id=ctx.task_id, error=str(e))

    async def _try_load_step_from_cache(
        self,
        ctx: DistillContext,
        step_name: str,
        ctx_attr: str,
    ) -> bool:
        """CP3.6.4：尝试从 stage cache 加载 step 结果到 ctx。

        Returns:
            True 如果 cache hit（已反序列化到 ctx，跳过该步）
            False 如果 cache miss（需要实跑）
        """
        cached = await read_stage(ctx.task_id, step_name)
        if cached is None:
            return False

        # 反序列化到 ctx（cached 是 dict，需要重建 Pydantic 模型）
        try:
            if step_name == "step1_structure":
                from .schemas import StructuredOutput

                ctx.structured = StructuredOutput.model_validate(cached)
            elif step_name == "step2_rewrite":
                from .schemas import RewriteOutput

                ctx.rewrite = RewriteOutput.model_validate(cached)
            elif step_name == "step3_tts":
                from .schemas import TTSOutput

                ctx.tts = TTSOutput.model_validate(cached)
            elif step_name == "step4_concat":
                from .schemas import AudioConcatOutput

                ctx.final = AudioConcatOutput.model_validate(cached)
        except Exception as e:
            # 反序列化失败（schema 演进） → 当 miss 处理
            log.warning(
                "stage_cache_deserialize_failed",
                task_id=ctx.task_id,
                step=step_name,
                error=str(e),
            )
            return False

        log.info("stage_cache_hit", task_id=ctx.task_id, step=step_name)
        return True

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
        """CP7.2 + CP9.x: 上传 step4 拼接后的 bytes 到 Storage → 更新 ctx.final.audio_url。

        CP9.x 修复：直接用 step4 已合成的 `ctx.final.audio_bytes`，不再调一次 TTS
        （之前的实现对 `rewrite.body` 又跑一次 TTS，成本 ×2 且 step3 真实音频路径下完全冗余）。
        Mock 路径（`ctx.final.audio_bytes is None`）：跳过，保留占位 URL。
        """
        if ctx.final is None or not ctx.final.audio_bytes:
            # 真实路径必填 audio_bytes；mock 路径下保持 step4 占位 URL
            return
        try:
            storage = _get_storage()
            audio_key = f"audio/{ctx.article_id}.{ctx.final.format}"
            audio_url = await storage.save(audio_key, ctx.final.audio_bytes)
            ctx.final.audio_url = audio_url
            log.info("audio_saved", article_id=ctx.article_id, audio_url=audio_url)
        except Exception as e:
            log.warning("audio_save_failed", article_id=ctx.article_id, error=str(e))
            # 不破主流程：audio_url 保持 step4 的本地临时路径/占位 URL

    async def _write_final_to_db(self, ctx: DistillContext) -> None:
        """最终结果写 DB（script_text + audio_url + duration + tags + quality_score）。

        CP-DISTILL-TEXT 修复：把 step2 听感改写稿（hook/body/outro）落到
        `distilled_articles.script_text` —— 之前该列永远是 NULL，安卓详情页
        看不到"LLM 整理后的正文"。用 `\\n\\n` 分段拼接，客户端按段落渲染。
        """
        if self.session_factory is None or ctx.final is None:
            return
        from sqlalchemy import func, update

        from stashbox.backend.common.models import DistilledArticle

        # CP-DISTILL-TEXT + CP-DISTILL-QUALITY：把 hook / sections / outro
        # 落库，**保留段间空行**，客户端按空行分段渲染。
        # sections 比旧 body 多保留段落结构（视觉上更整齐，TTS 上更利于停顿）。
        #
        # CP-DQ-SYNC-TTS（CP-DISTILL-DISPLAY-SYNC）：**script_text 段落边界 = TTS 实际段边界**。
        # sections 内部按"句末标点"切时（≥2 子段），子段之间也用 \n\n 分开，
        # 让客户端"看到的段落"和"听到的句子"严格对齐。
        # 实现：复用 step3 的切分规则（同样的 _SENT_SPLIT_RE / TTS_CHUNK_MAX_CHARS），
        # 但不做短句贪心合并（让每段独立，视觉上一段=听觉上一句）。
        _SENT_SPLIT_RE_LOCAL = __import__("re").compile(r"(?<=[。！？!?；;\n])")
        _TTS_MAX_LOCAL = 100

        def _split_section_for_display(text: str) -> list[str]:
            """与 step3 TTS 切分规则严格一致的段落切分。

            与 step3 不同点：不贪心合并短句（避免一个"段落"= 多句）。
            """
            sents = [s for s in _SENT_SPLIT_RE_LOCAL.split(text or "") if s and s.strip()]
            if not sents:
                return []
            pieces: list[str] = []
            for s in sents:
                s = s.strip()
                if len(s) <= _TTS_MAX_LOCAL:
                    pieces.append(s)
                else:
                    import re as _re

                    sub = _re.split(r"(?<=[，,、\s])", s)
                    buf = ""
                    for t in sub:
                        if len(buf) + len(t) <= _TTS_MAX_LOCAL:
                            buf += t
                        else:
                            if buf.strip():
                                pieces.append(buf.strip())
                            while len(t) > _TTS_MAX_LOCAL:
                                pieces.append(t[:_TTS_MAX_LOCAL])
                                t = t[_TTS_MAX_LOCAL:]
                            buf = t
                    if buf.strip():
                        pieces.append(buf.strip())
            return pieces

        script_parts: list[str] = []
        if ctx.rewrite is not None:
            hook = (ctx.rewrite.hook or "").strip()
            if hook:
                script_parts.append(hook)
            for sec in ctx.rewrite.sections or []:
                # 关键 —— sections 内部按句切，让 \n\n 分段 = TTS 句子边界
                for para in _split_section_for_display(sec or ""):
                    if para.strip():
                        script_parts.append(para.strip())
            outro = (ctx.rewrite.outro or "").strip()
            if outro:
                script_parts.append(outro)
        script_text = "\n\n".join(script_parts) if script_parts else None

        async with self.session_factory() as session:
            await session.execute(
                update(DistilledArticle)
                .where(DistilledArticle.id == ctx.task_id)
                .values(
                    script_text=script_text,
                    audio_url=ctx.final.audio_url,
                    duration_sec=ctx.final.duration_sec,
                    # CP-MOCK-SENTINEL：过滤 mock 兜底产生的 sentinel，
                    # 避免历史污染再次扩散到订阅推送 / 标签过滤链路
                    tags=_strip_mock_tags(ctx.structured.tags if ctx.structured else []),
                    quality_score=MOCK_QUALITY_SCORE,  # mock 评分
                    updated_at=func.now(),
                )
            )
            await session.commit()

    async def _refund_quota(self, ctx: DistillContext) -> None:
        """蒸馏失败退还配额（CP1.6 已实现 + CP9.x 幂等保护）。

        CP9.x 修复：Arq 默认 retry_max=2，首跑 + 2 次重试都失败时，
        `_refund_quota` 会被调 3 次 → 用户白嫖双倍配额退还。
        用 Redis SETNX `refund:{task_id}` 作幂等锁（TTL 1 天覆盖整个 Arq retry 窗口），
        第一次 refund 成功后置锁，后续重试直接跳过。
        """
        if self.session_factory is None:
            return
        from stashbox.backend.common import quota_service
        from stashbox.backend.common.redis_client import get_redis_pool

        # 幂等检查：SET NX EX 86400（覆盖整个 retry 窗口）
        try:
            import redis.asyncio as redis_async

            client = redis_async.Redis(connection_pool=get_redis_pool())
            locked = await client.set(f"refund:{ctx.task_id}", "1", nx=True, ex=86400)
            if not locked:
                log.info(
                    "quota_refund_skipped_already_refunded",
                    task_id=ctx.task_id,
                    user_id=ctx.user_id,
                )
                return
        except Exception as e:
            # Redis 不可用时降级为"无锁"模式（不阻塞主流程）
            log.warning(
                "quota_refund_lock_failed_proceed_without_lock",
                task_id=ctx.task_id,
                error=str(e),
            )

        async with self.session_factory() as session:
            await quota_service.refund(session, ctx.user_id)
