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

import os

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from llm import get_llm_client, maybe_close_llm_client
from llm import reload as llm_reload
from stashbox.backend.app.services.tts import reload as tts_reload
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article
from stashbox.backend.common.models.tag import Tag, TagSubscription
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.models.distilled_article import DistilledArticle
from stashbox.backend.common.analytics import track_simple, track
from stashbox.backend.common.events import EventName

# CP-AGENT-RUNNER-INTEGRATION：distill_task.py 走 LangGraph agent（替代 DistillPipeline.run）
# 向后兼容：pipeline 仍保留（被外部测试 mock / 老 arq 重试 queue 里残留任务可能用到）
from agent.runner import agent_app as _agent_app
from agent.state import AgentState

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
        # CP-MOCK-SENTINEL：兜底过滤 mock 兜底标签（pipeline 已过滤，再加一层防御）
        article_tags = [a for a in article_tags if a != "__mock__"]
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

    # CP11.x：每个蒸馏任务启动前主动 reload TTS client，让 admin-web 改的 TTS 配置
    # （provider/api_key 等）通过 system_config 表 + Redis 5s 缓存 实时生效。
    # 否则 ai-service 进程的 _client 缓存不会感知 content-service admin_router PUT 后的 reload。
    try:
        await tts_reload()
    except Exception as exc:
        log.warning("tts_reload_failed_in_distill", error=str(exc))

    # CP11.x：蒸馏 LLM 同样对齐管理后台入口（system_config KEY_LLM，DB > env）。
    # reload 只刷配置 dict（client 每任务新建，无缓存），失败回落 env 不影响蒸馏。
    try:
        cfg = await llm_reload()
        log.info("llm_config_reloaded", provider=cfg.get("provider"), model=cfg.get("model"))
    except Exception as exc:
        log.warning("llm_reload_failed_in_distill", error=str(exc))

    # CP-DELETE 兜底：文章被删除后 arq 队列里的 task 还会跑。
    # 显式查 articles 行存在性，不存在 → 早退 + 一次性埋点 + 不抛 retry（避免重复埋 DISTILL_FAILED/RETRY）。
    async with AsyncSessionLocal() as db:
        article_exists = await db.scalar(select(Article.id).where(Article.id == article_id))
        if article_exists is None:
            log.warning(
                "arq_distill_skipped_article_missing",
                task_id=task_id,
                article_id=article_id,
            )
            # 一次性埋点：DISTILL_FAILED 一次（让数据完整，但不重试不重抛）
            async with AsyncSessionLocal() as db2:
                try:
                    await track(
                        db2,
                        EventName.DISTILL_FAILED,
                        user_id=user_id,
                        article_id=article_id,
                        reason="article deleted before distill",
                    )
                    await db2.commit()
                except Exception as exc:
                    log.warning("DISTILL_FAILED 埋点异常（被忽略）: err={exc}".format(exc=exc))
            return {"task_id": task_id, "status": "skipped_article_missing"}

    # CP-DISTILL 状态回写（2026-09-24 修）：
    # 每次执行（含 Arq retry）都置 articles.status='distilling'。
    # 此前只有 ai-service 派任务时（main.py）设一次，而失败路径从不回写，
    # 导致 LLM 限流等失败后文章永久停在 distilling —— 蒸馏中心一直显示
    # 「处理中」，看不到「失败/重试」入口，用户以为卡死。
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(
                update(Article).where(Article.id == article_id).values(status="distilling")
            )
            await db.commit()
        except Exception as exc:
            log.warning(
                "articles_status_distilling_write_err", article_id=article_id, error=str(exc)
            )

    # CP2 + CP3 打通：从 articles.raw_content JSONB 读真正文（替代占位文本）
    async with AsyncSessionLocal() as db:
        raw_content = await _load_raw_content(db, article_id)

    # CP-AGENT-RUNNER-INTEGRATION：distill 走 LangGraph agent（替代硬编码 pipeline）
    # - raw_content 直接当 fetched_content 喂入（避免真实 fetch_url 调外部网络）
    # - MemoryStore 从 PG 读 user_profile + few_shot_examples 注入 prompt
    # - agent_app.ainvoke() 跑完后 _persist_agent_final 写回 DB
    try:
        final = await _run_distill_via_agent(
            task_id=task_id,
            article_id=article_id,
            user_id=user_id,
            url=url,
            raw_content=raw_content,
            simulate_failure=simulate_failure,
        )
        # CP-AGENT-FAILURE-PROPAGATION：agent 失败（final.status != "done"）必须走
        # 失败路径（退还配额 + articles.status=failed + 抛异常给 Arq retry），
        # 否则任务会被误记为成功（旧 DistillPipeline 是靠抛异常表达失败的）。
        if (final or {}).get("status") != "done":
            err_kind = (final or {}).get("error_kind") or "internal"
            err_step = (final or {}).get("error_step") or "unknown"
            err_msg = (final or {}).get("error") or "agent 未返回 done"
            raise RuntimeError(f"agent distill failed at {err_step} ({err_kind}): {err_msg}")

        # CP10: 写回 articles.status=ready + audio_url（派生自 agent final state）
        async with AsyncSessionLocal() as db:
            # CP-AGENT-PERSIST-ARTICLE-KEY：这里必须**按 article_id 查**，
            # 不能只按 `id == task_id`。
            #
            # `_persist_agent_final` 按业务键（一篇一结果）落库：同一篇文章换新
            # task_id 重跑时会复用既有那行（id 仍是第一次的 task_id）。若这里按
            # `id == task_id` 查，就永远查不到 → `if da is not None` 整段跳过 →
            # articles.status 永远停在 distilling，audio_url 也写不回 Article。
            da_result = await db.execute(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            da = da_result.scalar_one_or_none()
            if da is None:  # 兼容只认 task_id 的老数据
                da_result = await db.execute(
                    select(DistilledArticle).where(DistilledArticle.id == task_id)
                )
                da = da_result.scalar_one_or_none()
            if da is not None:
                art_result = await db.execute(select(Article).where(Article.id == da.article_id))
                art = art_result.scalar_one_or_none()
                if (
                    art is not None
                    and da.audio_url
                    and da.audio_url.startswith(("http://", "https://"))
                ):
                    # CP9.x：必须有真 HTTP(S) URL 才标 ready；mock 路径 audio_url=""
                    # 会让文章保持 pending，等真实 TTS/ffmpeg 路径覆盖
                    art.status = "ready"
                    art.audio_url = da.audio_url
                    await db.commit()
                    log.info("articles.status_updated_to_ready", article_id=art.id)
                elif art is not None and da.audio_url:
                    # 非 HTTP URL（如本地临时路径）— 视为未上传，不标 ready
                    log.warning(
                        "articles.audio_url_not_http",
                        article_id=art.id,
                        audio_url=da.audio_url[:80],
                    )
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
        # feedback.reason 为 VARCHAR(64)，超长会触发 StringDataRightTruncationError，
        # 导致埋点失败（被忽略）且掩盖真实错误。截断到 60 字符并保留完整信息到 metadata。
        reason = str(e)[:60]
        async with AsyncSessionLocal() as db:
            await track(
                db, EventName.DISTILL_FAILED, user_id=user_id, article_id=article_id, reason=reason
            )
            await track_simple(db, EventName.DISTILL_QUOTA_REFUND, user_id, article_id)
            await db.commit()  # track() 只 flush 不 commit

        # CP-DISTILL 状态回写（2026-09-24 修）：失败必须把 articles.status 置为
        # failed，否则与 distilled_articles.status='failed' 不一致，UI 永远「处理中」。
        # 若 Arq 还会 retry，该任务下次进入时会把状态重新置回 distilling，
        # 因此这里不区分「是否最后一次尝试」也能收敛到正确终态。
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Article).where(Article.id == article_id).values(status="failed")
                )
                await db.commit()
        except Exception as exc:
            log.warning("articles_status_failed_write_err", article_id=article_id, error=str(exc))

        # CP-AGENT-QUOTA-REFUND：配额退还原先由 DistillPipeline._refund_quota 承担，
        # agent 路径绕过了 pipeline，必须在任务级补回（否则用户蒸馏失败白扣配额）。
        # Redis SETNX 幂等锁：Arq 默认 retry_max=2，避免首跑 + 重试多次退双倍。
        await _refund_quota_once(task_id=task_id, user_id=user_id)
        raise  # 让 Arq 走 retry 逻辑
    finally:
        # CP-AGENT-RUNNER-INTEGRATION：agent 路径下 LLM client 由 agent.runner 内部
        # 通过 _llm_module.get_llm_client() 获取（单例），这里拿同一个实例关闭。
        # 单例 client（factory `_shared=True`）→ close 是 no-op；
        # httpx 连接池由 factory.close_all_llm_clients() 在 lifespan shutdown 统一关闭。
        try:
            _llm_for_close = get_llm_client()
            await maybe_close_llm_client(_llm_for_close)
        except Exception:
            pass


async def _refund_quota_once(task_id: str, user_id: int) -> None:
    """CP-AGENT-QUOTA-REFUND：退还 1 次配额，Redis SETNX 保证幂等（跨 Arq retry）。

    与 DistillPipeline._refund_quota 同语义，但由任务层调用（agent 路径不经过 pipeline）。
    Redis 不可用时降级为无锁（不阻塞失败流程）。
    """
    from stashbox.backend.common import quota_service
    from stashbox.backend.common.redis_client import get_redis_pool

    try:
        import redis.asyncio as redis_async

        client = redis_async.Redis(connection_pool=get_redis_pool())
        locked = await client.set(f"refund:{task_id}", "1", nx=True, ex=86400)
        if not locked:
            log.info("quota_refund_skipped_already_refunded", task_id=task_id, user_id=user_id)
            return
    except Exception as exc:
        log.warning(
            f"quota_refund_lock_failed_proceed_without_lock task_id={task_id} error={exc!s}"
        )

    try:
        async with AsyncSessionLocal() as session:
            await quota_service.refund(session, user_id)
    except Exception as exc:
        log.warning(f"quota_refund_failed task_id={task_id} user_id={user_id} error={exc!s}")


# ---------------------------------------------------------------------------
# CP-AGENT-RUNNER-INTEGRATION：通过 LangGraph agent_app.ainvoke() 跑 distill
# ---------------------------------------------------------------------------


async def _run_distill_via_agent(
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    raw_content: str,
    source: str = "web",
    simulate_failure: bool = False,
) -> dict:
    """CP-AGENT-RUNNER-RUN：把 distill 走 LangGraph agent 跑（替代 DistillPipeline.run）。

    流程：
      1) Phase 4：尝试连接 MCP echo server，把 echo/reverse 加入 default_registry
         （失败 → 静默跳过，不影响主流程）
      2) 从 raw_content 拼 AgentState 初始值
      3) Phase 2：从 MemoryStore 读 user_profile + few_shot 注入到 state
      4) await agent_app.ainvoke(state)
      5) 把 final state 写回 articles / distilled_articles / feedback

    返回 final state dict 给 distill_task 用（决定 status='done'/'failed'）。
    """
    from datetime import datetime, timezone

    # CP-AGENT-SIMULATE-FAILURE：simulate_failure=True 走真实失败路径
    # （FAILED + 退还配额 + 抛异常给 Arq retry），供 D9 e2e / 手工验证用。
    if simulate_failure:
        raise RuntimeError("simulated distill failure")

    # Phase 4：MCP server 接入（CP-AGENT-MCP）。
    # 默认关闭：每个 distill 任务起一个 MCP 子进程成本高（启动 + handshake ~200ms），
    # 且当前 echo echo server 只是示例。生产接入真实 MCP server 时设
    #    ENABLE_MCP_TOOLS=1
    # 注意：anyio 在 cancel scope mismatch 时抛 RuntimeError（继承 BaseException），
    # 所以 except 必须 catch BaseException 才能稳定吞掉 mcp 启动期错误。
    if os.getenv("ENABLE_MCP_TOOLS", "0").lower() in {"1", "true", "yes"}:
        try:
            from agent.mcp_client import (
                MCPClient,
                register_mcp_tools_to_registry,
                get_echo_mcp_server_command,
            )

            async with MCPClient(server_command=get_echo_mcp_server_command()) as mcp:
                count = await register_mcp_tools_to_registry(mcp, prefix="mcp_")
                log.info("mcp_tools_registered count=%d", count)
        except BaseException as exc:
            log.info(f"mcp_tools_skipped reason={type(exc).__name__}: {exc!s}")

    # Phase 2：MemoryStore 读 user_profile + few_shot
    from agent.memory import MemoryStore

    store = MemoryStore(session_factory=AsyncSessionLocal)
    user_profile = await store.load_user_profile(user_id)
    few_shot_examples = await store.load_few_shots(topic=source, limit=3)

    initial_state: AgentState = {
        "article_id": article_id,
        "user_id": user_id,
        "url": url,
        "source": source,
        "current_step": "fetch",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": task_id,
        # Phase 2：memory 注入
        "user_profile": {
            "user_id": user_profile.user_id,
            "tier": user_profile.tier,
            "ab_group": user_profile.ab_group,
            "preferences": user_profile.preferences,
        },
        "few_shot_examples": [
            {
                "input_excerpt": ex.input_excerpt,
                "output_excerpt": ex.output_excerpt,
                "score": ex.score,
                "tags": ex.tags,
            }
            for ex in few_shot_examples
        ],
        # Phase 1 简化：raw_content 直接当 fetched_content 喂进 rewrite
        # （避免真实 fetch_url 工具调外部网络；agent 后续可换 tool_registry.invoke）
        "fetched_content": raw_content,
    }

    log.info(
        "agent_run_started",
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        memory_count=len(few_shot_examples),
    )
    final = await _agent_app.ainvoke(initial_state)
    log.info(
        "agent_run_completed",
        task_id=task_id,
        article_id=article_id,
        status=final.get("status"),
        error_kind=final.get("error_kind"),
    )

    # 把 final state 写回 DB
    await _persist_agent_final(task_id, article_id, user_id, final)
    return final


async def _persist_agent_final(
    task_id: str,
    article_id: str,
    user_id: int,
    final: dict,
) -> None:
    """CP-AGENT-RUNNER-PERSIST：把 agent final state 写回 distilled_articles + feedback。

    CP-AGENT-PERSIST-ARTICLE-KEY（实测修复）：
    `distilled_articles.article_id` 上有 UNIQUE 约束（`distilled_articles_article_id_key`），
    即「一篇文章只能有一条蒸馏结果」。旧代码只按 `id == task_id` 查行，于是：

      - Arq 重试（task_id 不变）→ 命中旧行，正常；
      - 同一篇文章换新 task_id 重跑（如我们反复验证同一篇文章）→ 查不到 →
        INSERT → 撞唯一约束 → 整个事务被标记 rollback →
        随后的 `db.commit()` 抛 `PendingRollbackError` →
        **已经成功生成的 rewritten_script 一起被回滚丢弃**。

    现象是日志里 `agent_persist_failed ... This Session's transaction has been
    rolled back due to a previous exception`，而 LLM 改写的钱白花了。

    修法两处：
      1. 先按业务键 `article_id` 找行（真正的一篇一结果），找不到再按 `id == task_id`，
         兜底才新建；
      2. 数据行先 commit，埋点（DISTILL_FAILED）挪到**独立 session** 事后写，
         埋点失败绝不能再回滚业务数据。
    """
    status = final.get("status") or "failed"

    # ---- 第一步：写业务数据（独立事务，优先级最高，绝不被埋点牵连）----
    async with AsyncSessionLocal() as db:
        try:
            # 业务键优先：一篇文章一条蒸馏结果
            da_result = await db.execute(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            da = da_result.scalar_one_or_none()
            if da is None:
                # 兼容只认 task_id 的老数据
                da_result = await db.execute(
                    select(DistilledArticle).where(DistilledArticle.id == task_id)
                )
                da = da_result.scalar_one_or_none()
            if da is None:
                da = DistilledArticle(
                    id=task_id,
                    article_id=article_id,
                    status="queued",  # 占位，下面会按 final 状态覆盖
                )
                db.add(da)

            # 写 agent 产出（DistilledArticle 真实列：status/script_text/audio_url/duration_sec）
            da.status = "done" if status == "done" else "failed"
            if final.get("rewritten_script"):
                da.script_text = final["rewritten_script"]
            if final.get("tts_audio_url"):
                da.audio_url = final["tts_audio_url"]
            if final.get("tts_duration_sec") is not None:
                da.duration_sec = int(final["tts_duration_sec"])

            # 先 flush 让 IntegrityError 在这里暴露（而不是拖到 commit 把事务搞废）
            await db.flush()
            await db.commit()
        except Exception as exc:
            log.warning(
                "agent_persist_failed",
                task_id=task_id,
                article_id=article_id,
                error=str(exc),
            )
            await db.rollback()
            return  # 数据没落上就没必要再写埋点了

    # ---- 第二步：埋点（独立 session，失败只丢埋点，不影响上面的数据）----
    # 失败 → 带 reason，让 admin dashboard 看到"为什么失败"。
    # 注意：DistilledArticle 没有 metadata 列（见模型定义），错误详情
    # 通过 feedback（DISTILL_FAILED.reason）持久化，不写在该行上。
    if status == "failed" and final.get("error"):
        async with AsyncSessionLocal() as fb_db:
            try:
                await track(
                    fb_db,
                    EventName.DISTILL_FAILED,
                    user_id=user_id,
                    article_id=article_id,
                    reason=str(final["error"])[:200],
                )
                await fb_db.commit()
            except Exception as exc:
                log.warning(
                    "agent_persist_feedback_failed",
                    task_id=task_id,
                    error=str(exc),
                )
                await fb_db.rollback()
