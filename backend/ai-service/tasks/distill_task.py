"""Arq worker task：蒸馏任务入口（被 Arq worker 进程调）。

与 BackgroundTasks.add_task 的差异：
- 这里拿不到 FastAPI 的 Request / Depends —— 一切依赖从 ctx 显式传
- 数据库 session 自己开（AsyncSessionLocal）
- 失败抛异常 → Arq 自动 retry（按 retry_max）

CP3.5-pre-3 说明：
- 4 步流水线本身是 CP3.5-pre-2 的 DistillPipeline，本文件只做「参数 → DistillContext」的适配
- CP3-CONTENT：raw_content 不再用占位文本，改从 articles.raw_content JSONB 读
  （CP2.5 / CP-CREATE-ARTICLE 抓完落库的 FetchResult），Step 1 拿到的是真正文
"""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from distill import DistillContext, DistillPipeline
from llm import get_llm_client
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article
from stashbox.backend.common.models.tag import Tag, TagSubscription
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.models.distilled_article import DistilledArticle
from stashbox.backend.common.analytics import track_simple, track
from stashbox.backend.common.events import EventName

log = structlog.get_logger("ai-worker")


async def _trigger_subscription_pushes(
    db: AsyncSession,
    article_id: str,
    exclude_user_id: int,
) -> int:
    """蒸馏完成触发订阅推送（CP5.4b）。

    1. 读 distilled_articles.tags（蒸馏时写入的标签，name 列表如 ["科技","商业"]）
    2. 对每个 tag，查 tag_subscriptions 找 user_ids
    3. 排除 exclude_user_id（自己蒸馏不推自己）
    4. 批量 INSERT push_notifications 行
    5. 失败不破主流程

    Returns: 写入的推送数
    """
    try:
        # 1. 读 DistilledArticle（id 是 str: dst_xxx）
        da = await db.get(DistilledArticle, article_id)
        if not da or not da.tags:
            return 0

        # DistilledArticle 无 title 字段，需从 Article 表查
        article_title = "新文章"
        if da.article_id:
            art = await db.get(Article, da.article_id)
            if art and art.title:
                article_title = art.title[:50]

        article_tags = da.tags if isinstance(da.tags, list) else []
        if not article_tags:
            return 0

        # 2. da.tags 存的是 name（"科技"/"财经"），需映射为 slug（"tech"/"finance"）
        tag_slugs_result = await db.execute(select(Tag.slug).where(Tag.name.in_(article_tags)))
        tag_slugs = [row[0] for row in tag_slugs_result.fetchall()]
        if not tag_slugs:
            return 0

        # 3. 查订阅了这些 tag 的 user_ids（去重 + 排除 exclude_user_id）
        subs_result = await db.execute(
            select(TagSubscription.user_id)
            .where(TagSubscription.tag_id.in_(select(Tag.id).where(Tag.slug.in_(tag_slugs))))
            .where(TagSubscription.user_id != exclude_user_id)
            .distinct()
        )
        subscriber_ids = [row[0] for row in subs_result.fetchall()]
        if not subscriber_ids:
            return 0

        # 4. 批量 INSERT push_notifications
        # 第一个 tag 用于显示推送来源
        primary_tag_slug = tag_slugs[0]
        notifs = [
            PushNotification(
                user_id=sub_id,
                article_id=article_id,  # DistilledArticle.id (dst_xxx)
                tag_slug=primary_tag_slug,
                title=f"新文章：{article_title[:50]}",
                body=f"你订阅的 {primary_tag_slug} 标签有新文章蒸馏完成",
                deeplink=f"/articles/{article_id}",
            )
            for sub_id in subscriber_ids
        ]
        db.add_all(notifs)
        await db.commit()
        return len(notifs)
    except Exception as e:
        # 失败不破主流程
        log.warning(
            "subscription_push_trigger_failed",
            article_id=article_id,
            error=str(e),
        )
        return 0


class ArticleNotFoundError(Exception):
    """文章不存在（让 Arq 走 retry_max 次后失败）。"""

    pass


class _FailingLLM:
    """simulate_failure=True 时的 LLM：任何 chat 都抛错。

    让「模拟失败」走和真实失败完全一样的路径（pipeline → FAILED + 退还配额 + 抛异常），
    避免 main.py 里再维护一套失败分支。
    """

    async def chat(self, req):
        raise RuntimeError("simulated distill failure")

    async def close(self):
        return None


async def _load_raw_content(db: AsyncSession, article_id: str) -> str:
    """从 articles.raw_content JSONB 读 content_text（CP2 + CP3 打通）。

    优先级：
    1. raw_content["content_text"] (CP-CREATE-ARTICLE 写入的 FetchResult)
    2. fallback: raw_content["title"] + raw_content["url"] + "[无正文]"
    3. 文章不存在 / raw_content 为空 → "[empty article]"

    Raises:
        ArticleNotFoundError: 文章不存在（让 Arq 走 retry）
    """
    art = await db.scalar(select(Article).where(Article.id == article_id))
    if art is None:
        raise ArticleNotFoundError(f"article {article_id} not found")

    raw = art.raw_content
    if not isinstance(raw, dict):
        return f"[empty article] title={art.title or '(无标题)'} url={art.url}"

    content_text = raw.get("content_text")
    if content_text and content_text.strip():
        return content_text

    # fallback：标题 + URL
    title = raw.get("title") or art.title or "(无标题)"
    url = raw.get("url") or art.url
    return f"[无正文] title={title} url={url}"


async def distill_task(
    ctx: dict,
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    title: str | None = None,
    simulate_failure: bool = False,
) -> dict:
    """Arq worker task：跑 DistillPipeline。

    Args:
        ctx: Arq 提供的 worker context（含 redis / job_id）
        task_id: distilled_articles.id
        article_id: articles.id
        user_id: 用户 ID
        url: 文章 URL
        title: 文章标题
        simulate_failure: 模拟失败（走真实失败路径：FAILED + 退还配额 + 抛异常给 Arq retry）

    Raises:
        ArticleNotFoundError: articles 里查不到 article_id（交给 Arq retry）
    """
    log.info("arq_distill_started", task_id=task_id, article_id=article_id)

    # CP2 + CP3 打通：从 articles.raw_content JSONB 读真正文（替代占位文本）
    async with AsyncSessionLocal() as db:
        raw_content = await _load_raw_content(db, article_id)

    pipeline_ctx = DistillContext(
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        url=url,
        title=title,
        raw_content=raw_content,  # ← 真抓的内容（or fallback），不是占位文本
    )

    llm = _FailingLLM() if simulate_failure else get_llm_client()
    pipeline = DistillPipeline(llm=llm, db_session_factory=AsyncSessionLocal)

    try:
        await pipeline.run(pipeline_ctx)
        # CP10: 写回 articles.status=ready + audio_url（派生自 distilled_articles）
        async with AsyncSessionLocal() as db:
            da_result = await db.execute(
                select(DistilledArticle).where(DistilledArticle.id == task_id)
            )
            da = da_result.scalar_one_or_none()
            if da is not None:
                art_result = await db.execute(select(Article).where(Article.id == da.article_id))
                art = art_result.scalar_one_or_none()
                if art is not None and da.audio_url:
                    art.status = "ready"
                    art.audio_url = da.audio_url
                    await db.commit()
                    log.info("articles.status_updated_to_ready", article_id=art.id)
        # CP6.2.2.2b 埋点：DISTILL_STEP_COMPLETE（注：步骤在 DistillPipeline 内部迭代，
        # 本文件只在外层 pipeline.run 完成后打点；如需真正 per-step 打点需改 DistillPipeline）
        async with AsyncSessionLocal() as db:
            await track(
                db,
                EventName.DISTILL_STEP_COMPLETE,
                user_id=user_id,
                article_id=article_id,
                metadata={"step": "pipeline_run"},
            )
            await db.commit()  # track() 只 flush 不 commit
        log.info("arq_distill_completed", task_id=task_id, article_id=article_id)
        # CP6.2.1 埋点：distill_completed
        async with AsyncSessionLocal() as db:
            await track_simple(db, EventName.DISTILL_COMPLETED, user_id, article_id)
            await db.commit()  # track() 只 flush 不 commit
        # CP5.4b：蒸馏完成触发订阅推送
        async with AsyncSessionLocal() as db:
            await _trigger_subscription_pushes(
                db,
                article_id=article_id,
                exclude_user_id=user_id,
            )
        return {"task_id": task_id, "status": "done"}
    except Exception as e:
        log.exception("arq_distill_failed", task_id=task_id, article_id=article_id, error=str(e))
        # CP6.2.2.2b 埋点：DISTILL_RETRY（注：Arq 自动 retry，distill_task.py 内无显式 retry 计数器，
        # 此处代表 Arq 将在该异常抛出后触发重试）
        async with AsyncSessionLocal() as db:
            await track(
                db,
                EventName.DISTILL_RETRY,
                user_id=user_id,
                article_id=article_id,
                metadata={"reason": str(e)},
            )
            await db.commit()  # track() 只 flush 不 commit
        # CP6.2.1 埋点：distill_failed + distill_quota_refund
        async with AsyncSessionLocal() as db:
            await track(
                db, EventName.DISTILL_FAILED, user_id=user_id, article_id=article_id, reason=str(e)
            )
            await track_simple(db, EventName.DISTILL_QUOTA_REFUND, user_id, article_id)
            await db.commit()  # track() 只 flush 不 commit
        raise  # 让 Arq 走 retry 逻辑
    finally:
        await llm.close()
