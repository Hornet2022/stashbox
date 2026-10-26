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
from arq import Retry as ArqRetry
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from llm import get_llm_client, maybe_close_llm_client
from llm import reload as llm_reload
from arq_settings import load_arq_config
from services.content_client import fill_article_captions
from stashbox.backend.app.services.tts import reload as tts_reload
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common import cache_service
from stashbox.backend.common import media_placeholders as placeholders
from stashbox.backend.common.models import Article
from stashbox.backend.common.models.tag import Tag, TagSubscription
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.models.distilled_article import DistilledArticle
from stashbox.backend.common.analytics import track_simple, track
from stashbox.backend.common.events import EventName
from stashbox.backend.common.distill_status import mark_distill_task_failed

# CP-AGENT-RUNNER-INTEGRATION：distill_task.py 走 LangGraph agent（替代 DistillPipeline.run）
# 向后兼容：pipeline 仍保留（被外部测试 mock / 老 arq 重试 queue 里残留任务可能用到）
from agent.runner import agent_app as _agent_app
from agent.state import AgentState

log = structlog.get_logger("ai-worker")


class DistillRetry(ArqRetry):
    """带原始失败原因的 arq.Retry。

    为什么不能直接抛 `arq.Retry`：

    arq 0.28 的 `Retry.__init__(self, defer=None)` **不接受 message**，
    且 `__str__` 固定返回 `'<Retry defer 15.00s>'`。直接抛它的话，
    arq worker 打出来的失败日志就是

        73.82s ! xxx:distill_task failed, RuntimeError: <Retry defer 15.00s>

    **真实失败原因（"tts_synthesize 失败: OpenAITTS 超时" 之类）整个消失** ——
    排障时只能去翻异常的 __cause__，而 arq 的结构化日志并不带 __cause__。

    这里把原因留在 str() 里：既是 RuntimeError 的子类（现有调用方的
    `except RuntimeError` / `pytest.raises(RuntimeError)` 都不受影响），
    又满足 arq 的 `isinstance(e, Retry)` 判定 → 照样触发重试。
    """

    def __init__(self, message: str, defer: int = 15):
        super().__init__(defer=defer)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _retry_after_seconds(exc: BaseException, default: int = 15) -> int:
    """从异常链里找 `retry_after`（熔断器告诉调用方「多久后再来」）。

    为什么要沿链找：熔断异常在 TTS client 里抛出，中间会被
    ToolError / RuntimeError 逐层包起来，只有**链底**那个还带着
    `retry_after` 属性。找不到就用默认的 15s。
    """
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 10:
        value = getattr(cur, "retry_after", None)
        if value not in (None, ""):
            try:
                return max(1, int(float(value)))
            except (TypeError, ValueError):
                break
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return default


async def _filter_known_tag_names(db: AsyncSession, names: list[str]) -> list[str]:
    """只保留 tags 表里**真实存在**的标签名。

    订阅推送按 `Tag.name.in_(da.tags)` 精确匹配 —— 表里没有的名字写进去
    只是垃圾（查不到 → 推送不出），还会让「为什么没推送」变成查不到原因。

    查不到表 / 出错 → **原样返回**（宁可多存，也别把标签丢了 ——
    标签是订阅推送的唯一入口）。
    """
    try:
        from stashbox.backend.common.models.tag import Tag

        rows = await db.execute(select(Tag.name).where(Tag.name.in_(names)))
        known = {row[0] for row in rows.fetchall()}
        # 保持 LLM 给的顺序（第一个当主标签）
        return [n for n in names if n in known]
    except Exception as exc:
        log.warning("tag_filter_failed", error=str(exc))
        return names


async def _load_tag_vocabulary() -> str:
    """拉一次 tags 表的候选标签清单（供 LLM 逐字选）。

    复用 pipeline 的那一份（`ai-service/distill/tag_vocab_cache`，Redis 缓存 5min）——
    **不另造一套**：两处各拉一次就会有两个口径，而标签名必须逐字一致
    （推送按 `Tag.name.in_(da.tags)` 精确匹配）。

    拉不到就返回空串 → prompt 里给兜底文案 → LLM 不输出 tags。
    失败绝不破主流程（蒸馏比标签重要）。
    """
    try:
        # ai-service 目录在 sys.path 上（与本文件里 `from distill.agent_hook_bridge …` 同惯例）
        from distill.tag_vocab_cache import load_tag_vocabulary as _load

        async with AsyncSessionLocal() as db:
            return await _load(db)
    except Exception as exc:
        log.warning("tag_vocabulary_load_failed", error=str(exc))
        return ""


async def _trigger_subscription_pushes(
    db: AsyncSession,
    article_id: str,
    exclude_user_id: int,
) -> int:
    """蒸馏完成触发订阅推送（CP5.4b）。

    1. 按 articles.id 查 distilled_articles 行，读它蒸馏时写入的标签
       （name 列表如 ["科技","商业"]）
    2. name → slug 映射，查 tag_subscriptions 找 user_ids
    3. 排除 exclude_user_id（自己蒸馏不推自己）
    4. 批量 INSERT push_notifications 行
    5. 失败不破主流程

    Returns: 写入的推送数

    id 口径（真机排查出来的两处错配）
    ----------------------------------
    调用方（[distill_task]）传的 `article_id` 是 **articles.id**（art_xxx），
    而这里原来 `db.get(DistilledArticle, article_id)` 是按 **主键**
    distilled_articles.id（dst_xxx）查 —— 传进来必然查不到，函数每次都在
    第一行 return 0。

    就算把调用方改成传 dst_xxx 让第 1 步查得到，第 4 步 INSERT 又会炸：
    push_notifications.article_id 上有指向 **articles(id)** 的外键
    （push_notifications_article_id_fkey，ON DELETE CASCADE），塞 dst_xxx
    直接 ForeignKeyViolationError；又被下面的 except 吞成一行 warning。

    也就是说这个函数**没有任何一种入参能跑通** —— 这就是管理后台「推送队列」
    永远空着的根因。现在两处都按 articles.id 统一。
    """
    try:
        # 1. 按 articles.id 找蒸馏结果（不是按 DistilledArticle 主键）
        da = await db.scalar(
            select(DistilledArticle).where(DistilledArticle.article_id == article_id)
        )
        if not da:
            return 0

        # DistilledArticle 无 title 字段，需从 Article 表查
        article_title = "新文章"
        art = None
        if da.article_id:
            art = await db.get(Article, da.article_id)
            if art and art.title:
                article_title = art.title[:50]

        # 0041 共享语义：**用户共享的标签也要触发订阅推送**。
        # 订阅流/读侧授权按 `da.tags ∪ shared_tags` 合并，推送是订阅闭环的
        # **入流一环**（先有推送，用户才点进来看），必须同一口径 ——
        # 否则用户刚共享一篇，订阅者既收不到推送、流里也看不到（不一致）。
        # 共享标签可能不在 tags 表（第 2 步映射 slug 时自然过滤掉），不影响。
        article_tags = da.tags if isinstance(da.tags, list) else []
        shared_tags = getattr(art, "shared_tags", None) if art else None
        if isinstance(shared_tags, list):
            article_tags = list(dict.fromkeys([*article_tags, *shared_tags]))
        # CP-MOCK-SENTINEL：兜底过滤 mock 兜底标签（pipeline 已过滤，再加一层防御）
        article_tags = [a for a in article_tags if a != "__mock__"]
        if not article_tags:
            return 0

        # 2. da.tags 存的是 name（"科技"/"财经"），需映射为 slug（"tech"/"finance"）
        #    ⚠️ 同时把 name 一起取出来：推送正文要给用户看**中文名**，
        #    slug（"tech"）是内部检索键，直接给人看像 bug。
        tag_rows = await db.execute(select(Tag.name, Tag.slug).where(Tag.name.in_(article_tags)))
        tag_pairs = [(row[0], row[1]) for row in tag_rows.fetchall()]
        if not tag_pairs:
            return 0
        tag_slugs = [slug for _, slug in tag_pairs]
        primary_tag_name = tag_pairs[0][0]
        primary_tag_slug = tag_slugs[0]

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
                # 必须是 articles.id：push_notifications_article_id_fkey 指向
                # articles(id)。写 dst_xxx 会 ForeignKeyViolation（见函数 docstring）
                article_id=article_id,
                tag_slug=primary_tag_slug,
                title=f"新文章：{article_title[:50]}",
                # 给用户看**中文标签名**，不是内部 slug（"你订阅的 tech 标签"像 bug）
                body=f"你订阅的 {primary_tag_name} 标签有新文章蒸馏完成",
                # ⚠️ deeplink 必须是安卓 NavHost 认的那个 scheme：
                # 安卓注册的是 `stashbox://detail/{id}`（MainActivity.kt 的 navDeepLink），
                # 而这里原来写 `/articles/{id}` —— 安卓的解析器只认 `stashbox://article/`，
                # **三方各说各话**，于是用户点通知「只标已读、不跳转」。
                # 统一成 NavHost 真正注册的那个。
                deeplink=f"stashbox://detail/{article_id}",
                # 2026-10-21：status 严格表示**投递状态**。
                # 默认值原是 'sent'（"历史行已发出,无失败回执"），
                # 那等于**创建即宣称已送达**——而当时根本没有投递。
                # 现在新行一律 pending，等投递 worker 真的去发。
                status="pending",
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


class EmptyArticleContentError(RuntimeError):
    """文章没有可蒸馏的正文（2026-10-03）。

    为什么不复用 [ArticleNotFoundError]：走那条路 Arq 会**无限重试**，
    而"没正文"重试一万次还是没正文 —— 那是数据问题不是瞬时故障。
    这条会带着明确的失败原因落成 articles.status=failed，用户立刻看到。
    """


async def _load_raw_content(db: AsyncSession, article_id: str) -> str:
    """从 articles.raw_content JSONB 读 content_text（CP2 + CP3 打通）。

    优先级：
    1. raw_content["content_text"] (CP-CREATE-ARTICLE 写入的 FetchResult)
    2. 抛 [EmptyArticleContentError] —— **不再兜底成占位串**

    2026-10-03 改。原来的 fallback 链是：
        content_text → f"[无正文] title=… url=…" → f"[empty article] title=… url=…"
    也就是说**抓取失败建出来的空文章，会把这两句占位符当正文喂进 LLM**：
    烧 token、跑几分钟、产出垃圾或失败，而真正的原因（网络/风控/404）在
    建库那一步就被 `except` 吞了，永远传不到这里。

    现在内容拿不到就直接抛。剪藏入口（content-service）已经在建库前拦掉了
    绝大部分抓取失败，这里是给"绕过了入口"的其他路径兜底 —— 比如历史遗留的
    pending 文章、手工造的数据。宁可明确失败，也不要静默烧钱。
    """
    art = await db.scalar(select(Article).where(Article.id == article_id))
    if art is None:
        raise ArticleNotFoundError(f"article {article_id} not found")

    raw = art.raw_content
    if not isinstance(raw, dict):
        raise EmptyArticleContentError(
            f"article {article_id} raw_content is {type(raw).__name__}, not dict (抓取未成功)"
        )

    content_text = raw.get("content_text")
    if content_text and content_text.strip():
        return content_text

    title = raw.get("title") or art.title or "(无标题)"
    raise EmptyArticleContentError(
        f"article {article_id} content_text 为空 title={title} url={art.url}（抓取未取到正文）"
    )


async def _load_material_source(db: AsyncSession, article_id: str) -> str:
    """取**真实来源**（素材类型）："wechat_mp" / "document" / "pdf" / "generic_url" ...

    来源取自 `raw_content["source"]`（FetchResult 的 source 字段 = 真正解析它的
    fetcher/解析器），**不是** `articles.source`。

    ⚠️ 为什么不�� `articles.source`：那一列是**请求体里传的** source
    （`submit_article` 直接把 `req.source` 存进去），默认值 "web"，
    而实际来源是 fetcher 定的 —— 用它判断素材类型会**判错**
    （剪藏一个 PDF 直链，articles.source 是 "web"，但 raw_content.source 是 "pdf"；
    文档剪藏是 "document"）。

    ## 为什么单独一个函数而不改 `_load_raw_content` 的返回值

    `_load_raw_content` 是个**被广泛 mock 的契约**（多个测试直接替掉它）。
    改它的返回类型会把那些测试全变成「构造函数的守门员」——
    测的不是行为，而是「我有没有改签名」。

    这里用 `db.get()` 取同一 session 里已加载的 Article（identity map 命中，
    **不会**额外打 SQL），两个函数各管一件事。
    """
    try:
        art = await db.get(Article, article_id)
        raw = getattr(art, "raw_content", None) if art is not None else None
        if isinstance(raw, dict):
            return str(raw.get("source") or "unknown")
    except Exception as exc:  # noqa: BLE001 - 取不到来源不该毁掉整篇蒸馏
        log.warning("material_source_load_failed", article_id=article_id, error=str(exc))
    return "unknown"


async def _load_article_title(db: AsyncSession, article_id: str) -> str | None:
    """取本期标题（给标题播报用）。

    ## 为什么以 DB 为准，而不是用入队时带的 title

    入队参数 `title` 是**入队那一刻**的快照（`ai-service/main.py` 里
    `title = art.title`）。文章标题在入队之后被改（运营在后台改标题、
    剪藏时补标题）时，worker 手里那份就是旧的 —— 播报出一个已经不存在的标题。
    DB 里那份是当下的真相。

    入队参数仍然作为**兜底**（DB 查不到时用），两处都有才播报。

    ## 为什么取不到就返回 None 而不是编一个

    没有标题 = 不播报（`build_title_announcement` 会返回 None）。
    编一句「本期分享的标题是：（无标题）」比不播更糟。
    """
    try:
        art = await db.get(Article, article_id)
        title = getattr(art, "title", None) if art is not None else None
        # ⚠️ 只认 **str**：非 str（None / Mock / 其它类型）一律当"没有标题"。
        # 宽松的 `if title` 会让一个 Mock 对象变成标题的字符串化结果
        # （"<Mock id=...>"）—— 那会被原样念进音频里。
        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception as exc:  # noqa: BLE001 - 取不到标题不该毁掉整篇蒸馏
        log.warning("article_title_load_failed", article_id=article_id, error=str(exc))
    return None


async def distill_task(
    ctx: dict,
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    title: str | None = None,
    simulate_failure: bool = False,
    quota_charged: bool = True,
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
        quota_charged: 本次任务是否**已经从用户配额扣过一次**。

            True（默认，剪藏正常路径）—— content-service 剪藏入库时就扣了，
            蒸馏失败必须退，否则用户白扣。
            False（auto_retry 补偿重跑）—— 从没扣过，失败时就**不能**退，
            否则等于凭重试白赚配额。

            2026-10 修 P0：这两个退款点此前是无条件执行的。auto_retry 走的也是
            同一个 distill_task，于是「一次都没扣」的补偿重跑失败后照样退款 ——
            用户白赚额度。反过来若改成靠 task_id 是否 `retry_` 前缀去嗅探，
            等于把隐式约定当契约，换个调用方就再错一次。显式参数。

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

    # CP-DISTILL-CAPTION（2026-10-25）：**读正文之前**先补配图图注。
    #
    # 为什么在这里补：识图（下载图片 + 调 VLM）实测一篇 12 张图要 20~30s，
    # 放在剪藏请求里会让用户盯着转圈（内测反馈的「剪藏不成功」一半来自这里）
    # ⇒ 改成剪藏只留占位符、**蒸馏前**再补。
    #
    # 为什么补图注归 content-service 做：图片下载要走它那套 SSRF 防护与
    # 池化 client（复制一份到 ai-service 就是两份各自漂移），
    # 而图注要写回 `articles.raw_content`（它写的表）。
    #
    # ⚠️ 失败**不抛**：补图注失败只意味着「这篇没有图表信息」，
    # 正文照常蒸馏 —— 不能因为可有可无的图表把整篇稿子赔进去。
    # 第二道兜底：下面读到的正文会被 `strip_placeholders` 剥干净，
    # 绝不让占位符进 LLM。
    try:
        await fill_article_captions(article_id)
    except Exception as exc:  # noqa: BLE001 —— 双保险（client 内部已兜一层）
        log.warning("caption_fill_unexpected", article_id=article_id, error=str(exc))

    # CP2 + CP3 打通：从 articles.raw_content JSONB 读真正文（替代占位文本）
    #
    # 2026-10-03：拿不到正文时 **明确失败，不重试**。
    # 下面的通用 except 会 `raise` 交给 Arq retry（默认 retry_max=2），
    # 但"没正文"是数据问题不是瞬时故障 —— 重试两次也是同样的结果，
    # 每次都白跑一次状态回写。走下面的专用收口：置 failed + 退配额 + 不抛。
    # CP-TITLE-ANNOUNCE（2026-10-26）：本期标题（给音频开头播报用）。
    # 先给入队参数兜底，下面用 DB 里的现值覆盖。
    article_title = title
    try:
        async with AsyncSessionLocal() as db:
            raw_content = await _load_raw_content(db, article_id)
            source = await _load_material_source(db, article_id)
            # DB 为准、入队参数兜底 —— 理由见 `_load_article_title`。
            article_title = await _load_article_title(db, article_id) or title
    except EmptyArticleContentError as e:
        log.warning("arq_distill_skipped_empty_content", task_id=task_id, error=str(e))
        try:
            async with AsyncSessionLocal() as db:
                await track(
                    db,
                    EventName.DISTILL_FAILED,
                    user_id=user_id,
                    article_id=article_id,
                    reason="empty_article_content",
                    metadata={"error": str(e)[:200]},
                )
                await track_simple(db, EventName.DISTILL_QUOTA_REFUND, user_id, article_id)
                await db.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(status="failed", audio_url=None)
                )
                # 产物表**也要**落 failed（同一事务）。
                # 此前这条早退路径只写 articles ⇒ 产物表永远停在 'queued'：
                #  ① 用户点「重试蒸馏」被入队幂等守卫挡住（守卫只挡 queued/running）
                #     ⇒ 按钮按下去 15 分钟没反应（STALE_DISTILL_TASK_SEC 兜底才放行）
                #  ② admin 统计按 DistilledArticle.status='failed' 算失败数
                #     ⇒ 这类失败一次都没被算进去
                await mark_distill_task_failed(db, article_id)
                await db.commit()
        except Exception as track_exc:
            log.warning(
                "empty_content_mark_failed_err", article_id=article_id, error=str(track_exc)
            )
        # 配额照退：用户在剪藏时已经为这篇付过一次了。
        if quota_charged:
            await _refund_quota_once(task_id=task_id, user_id=user_id, article_id=article_id)
        else:
            log.info(
                "quota_refund_skipped_not_charged",
                task_id=task_id,
                user_id=user_id,
                reason="auto_retry_never_charged",
            )
        return {"task_id": task_id, "status": "failed", "reason": "empty_article_content"}

    # CP-DISTILL-CAPTION 兜底：正文里**绝不能**留占位符。
    #
    # 正常情况下补图注已经把占位符换成图注（或删掉）了，这里是**第二道防线**：
    # 补图注请求失败 / 端点 503 / 正文里本来就有历史脏数据时，
    # 至少保证 LLM 看到的是干净的正文 ——
    # `\x00IMG3\x00` 这种东西进了 prompt，模型会把它当正文的一部分胡编。
    #
    # ⚠️ 两种形态都剥（内存态 NUL 哨兵 + 落库态可见记法）：
    # 正文是从 JSONB 读出来的，可能是落库态。
    raw_content = placeholders.strip_placeholders(raw_content)

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
            source=source,
            # CP-TITLE-ANNOUNCE（2026-10-26）：标题进 agent state，
            # rewrite_node 用它拼开头那句「本期分享的标题是：…」。
            title=article_title,
            simulate_failure=simulate_failure,
        )
        # CP-AGENT-FAILURE-PROPAGATION：agent 失败（final.status != "done"）必须走
        # 失败路径（退还配额 + articles.status=failed + 抛异常给 Arq retry），
        # 否则任务会被误记为成功（旧 DistillPipeline 是靠抛异常表达失败的）。
        if (final or {}).get("status") != "done":
            err_kind = (final or {}).get("error_kind") or "internal"
            err_step = (final or {}).get("error_step") or "unknown"
            err_msg = (final or {}).get("error") or "agent 未返回 done"
            # CP-TTS-CIRCUIT-RETRY-AFTER：agent state 里的 retry_after
            # （熔断器给的「多久后再来」）要跟着异常一起传下去，
            # 否则下面的退避只会用固定 15s —— 15s 落在 60s 熔断窗口内，
            # Arq 的重试会全部打在「拒绝服务」上，把重试次数烧光。
            err = RuntimeError(f"agent distill failed at {err_step} ({err_kind}): {err_msg}")
            retry_after = (final or {}).get("retry_after")
            if retry_after:
                try:
                    err.retry_after = int(float(retry_after))
                except (TypeError, ValueError):
                    pass
            raise err

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

        # ── 判断「还有没有下一次尝试」 ────────────────────────────────
        #
        # Arq 的 ctx 带 job_try（1=首次执行，2/3=第 1/2 次重试），
        # max_tries = ARQ_RETRY_MAX + 1（arq_settings.load_arq_config）。
        # job_try < max_tries 说明**Arq 还会自动重试**。
        #
        # ⚠️ 为什么这个判断重要（2026-10-21 内测实测踩到）：
        #   原来这里**无条件**把 articles.status 标成 failed，注释写的是
        #   「下次进入会把状态重新置回 distilling，因此不区分是否最后一次
        #   尝试也能收敛到正确终态」—— 技术上确实收敛了，但**用户在这
        #   15~45 秒的窗口里看到的是「失败 / 换个源试试？点击重新蒸馏」**。
        #
        #   实测案例：一篇稿子 LLM 返回空 content → 首轮失败 → 标 failed →
        #   Arq 15 秒后重试 → **成功**（1052 字稿 / 250 秒音频 / 语速 4.21 正常）。
        #   用户截到的正是中间态那张「失败」页。
        #
        #   代价是实打实的：用户以为彻底失败 → 点「重试蒸馏」→ 排一个重复
        #   任务 → 白烧一次 LLM+TTS 费用、还可能撞配额上限。
        #
        # 所以：**还有下一次尝试时，状态保持 distilling**（用户看到「还在进行」，
        # App 的轮询会一直等到成功或最后一次失败）；只有最后一次尝试仍失败
        # 才标 failed（那时才是真的失败，「重试蒸馏」按钮该出现）。
        job_try = int(ctx.get("job_try") or 1)
        max_tries = load_arq_config().retry_max + 1
        will_retry = job_try < max_tries
        log.info(
            "arq_distill_attempt_failed",
            task_id=task_id,
            article_id=article_id,
            job_try=job_try,
            max_tries=max_tries,
            will_retry=will_retry,
            error=str(e)[:200],
        )

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
        if not will_retry:
            # 只有**最后一次**尝试仍失败才算「这次蒸馏真的失败了」。
            # 中间失败（还会重试）也记 DISTILL_FAILED 会让失败率统计虚高 ——
            # 实测一篇最终成功的稿子也会留下 1 条 failed 记录，报表上看就是「失败了」。
            async with AsyncSessionLocal() as db:
                await track(
                    db,
                    EventName.DISTILL_FAILED,
                    user_id=user_id,
                    article_id=article_id,
                    reason=reason,
                )
                await track_simple(db, EventName.DISTILL_QUOTA_REFUND, user_id, article_id)
                await db.commit()  # track() 只 flush 不 commit

        # CP-DISTILL 状态回写（2026-09-24 修）：失败必须把 articles.status 置为
        # failed，否则与 distilled_articles.status='failed' 不一致，UI 永远「处理中」。
        #
        # 2026-10-21：**还有下一次尝试时保持 distilling**（见上面 will_retry 的说明）——
        # 用户看到的是「还在进行」而不是「失败」，等 Arq 重试成功后直接变就绪。
        # 只有最后一次尝试仍失败才标 failed（那时「重试蒸馏」按钮才该出现）。
        #
        # 2026-10-21：同时清掉 articles.audio_url —— 它是成功路径写回产物时
        # 镜像 distilled_articles.audio_url 的（见上面的 art.audio_url = da.audio_url）。
        # 失败时留着它，文章就是 status='failed' 却还挂着一个「可播」的旧地址。
        # （中间态不清 —— 旧音频还能听，重跑成功后才被新音频替换。）
        try:
            async with AsyncSessionLocal() as db:
                if will_retry:
                    await db.execute(
                        update(Article)
                        .where(Article.id == article_id)
                        .values(status="distilling", error=str(e)[:200] or None)
                    )
                    # ⚠️ **产物表（distilled_articles）这里不动**。
                    #
                    # 曾试过把它改成 'running' 来表达「会重试」，但那有两个坏处：
                    #  ① 'running'/'queued' 在 IN_FLIGHT_DISTILL_STATUSES 里 →
                    #     幂等守卫会挡住用户手动重试，且 Arq 若没真正重试
                    #     （worker 崩溃 / Redis 丢消息）就**永久卡住** ——
                    #     用户既不能重试也不能重生成。
                    #  ② 内容服务侧的 _derive_status 现在按 articles.status 判
                    #     「还在进行」（articles.status=='distilling' 时不把
                    #     task.status=='failed' 当终态），所以产物表保持 failed
                    #     也不影响用户看到「正在重试」。
                    #
                    # 真正到最后一次仍失败时，articles.status='failed' +
                    # 产物表落 failed（见下面 else 分支）就够了 ——
                    # _derive_status 会回落到它。
                    #
                    # ⚠️ 注意：产物表在中间态**未必**是 queued —— agent 路径的
                    # `_persist_agent_final` 在 agent 返回非 done 时就写了
                    # 'failed'。这里不动它 ≠ 它保持 queued；读侧规则
                    # 「产物表 failed + articles 还在进行 ⇒ 以 articles 为准」
                    # 覆盖了这种情况（口径见 common/distill_status.py）。
                else:
                    await db.execute(
                        update(Article)
                        .where(Article.id == article_id)
                        .values(status="failed", audio_url=None, error=str(e)[:200] or None)
                    )
                    # 终局失败：**产物表也要落 failed**（同一事务）。
                    # 理由（实测症状，非洁癖）：
                    #  ① 用户点「重试蒸馏」被入队幂等守卫挡住 ——
                    #     守卫只放行产物表状态为 done/failed 的行，
                    #     产物表停在 'queued' 时要等到陈旧兜底
                    #     （STALE_DISTILL_TASK_SEC=900s）才放行
                    #     ⇒ 用户点了重试，15 分钟里什么都没发生。
                    #  ② admin 统计（failed_distillations_24h /
                    #     distill_success_rate）按 DistilledArticle.status
                    #     == 'failed' 算，非 agent 失败路径此前一次都算不到。
                    await mark_distill_task_failed(db, article_id)
                await db.commit()
        except Exception as exc:
            log.warning("articles_status_failed_write_err", article_id=article_id, error=str(exc))

        # CP-AGENT-QUOTA-REFUND：配额退还原先由 DistillPipeline._refund_quota 承担，
        # agent 路径绕过了 pipeline，必须在任务级补回（否则用户蒸馏失败白扣配额）。
        # Redis SETNX 幂等锁：Arq 默认 retry_max=2，避免首跑 + 重试多次退双倍。
        # quota_charged=False（auto_retry）：本来就没扣，退款等于白送额度。
        #
        # 2026-10-21：**只在最后一次尝试失败时才退**。
        # 原来每次失败就退（SETNX 保证只退一次）—— 若之后 Arq 重试成功，
        # 用户就白赚了 1 次配额（没真正拿到可用音频却退了钱）。
        # 「失败才退」要在**最终**失败时才兑现。
        # 中间态给客户端一个明确信号：「上一次尝试失败，正在重试」。
        # 阶段值走既有的 stage 机制（agent 的 current_step 那条链路），
        # 不另造一套状态字段。
        if will_retry:
            try:
                from stashbox.backend.common.distill_stage import set_stage

                await set_stage(article_id, "retry")
            except Exception as exc:  # noqa: BLE001
                log.debug("distill stage retry 上报失败 article=%s err=%s", article_id, exc)

        if quota_charged:
            if will_retry:
                log.info(
                    "quota_refund_deferred",
                    task_id=task_id,
                    user_id=user_id,
                    job_try=job_try,
                    reason="will_retry",
                )
            else:
                await _refund_quota_once(task_id=task_id, user_id=user_id, article_id=article_id)
        else:
            log.info(
                "quota_refund_skipped_not_charged",
                task_id=task_id,
                user_id=user_id,
                reason="auto_retry_never_charged",
            )

        # ── 抛 arq.Retry，让重试**真的**发生 ────────────────────────────
        #
        # ⚠️ 这里以前是裸 `raise`（把原始异常抛出去），在 **arq 0.28 下
        #    等于完全不重试** —— 整个自动兜底是空转的。
        #
        # arq 0.28 worker.py 的异常分支只认三种：
        #     if   retry_jobs and isinstance(e, Retry):              → 重试
        #     elif retry_jobs and isinstance(e, (CancelledError, RetryJob)): → 重试
        #     else: logger.exception(... failed ...) ; finish = True → **不重试**
        #
        # 普通异常（RuntimeError / TimeoutError / httpx 异常）全落进 else 分支，
        # 直接判 failed。而 max_tries / retry_jobs 配置是对的
        # （worker.py: retry_jobs = _cfg.retry_max > 0; max_tries = _cfg.retry_max + 1），
        # 注释里「抛异常给 Arq retry」、退款处的 SETNX 幂等锁
        # （"避免首跑 + 重试多次退双倍"）全都假设它会重试 —— 实际一次都没重试过。
        #
        # 实测（2026-10-21）：一篇长稿因 TTS 超时失败，Arq 只执行了 1 次，
        # 队列里没有第二次 arq_distill_started。
        #
        # 所以这里必须抛 arq 专用的 Retry 异常。
        #
        # defer=15s：实测上游（tertu.cn 的 vLLM）的问题有**时间聚集性** ——
        # 同一时刻连着几次请求连续返回空 content（实测 4 次连续全空），
        # 紧接着又完全正常。立刻重试大概率落在同一个坏窗口里，
        # 等 15s 再试才是有效的恢复策略。
        #
        # ⚠️ 但**熔断**情形例外：熔断窗口 60s，15s 的退避会落在窗口内，
        # Arq 的 5 次重试（5×15s=75s）大半都打在「拒绝服务」上 → 重试烧光。
        # 熔断异常带 retry_after=60 → 按它退避，一次窗口只烧一次重试。
        raise DistillRetry(str(e), defer=_retry_after_seconds(e)) from e
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


async def _make_refund_lock_client() -> "redis_async.Redis":  # noqa: F821
    """CP-AGENT-QUOTA-REFUND-SEAM：构造退款幂等锁用的 Redis 客户端。

    ## 为什么要单独抽一个函数（2026-10-03）

    原来 `_refund_quota_once` 里直接 `import redis.asyncio` 然后
    `redis_async.Redis(connection_pool=...)`。要测它就只能 monkeypatch
    `redis.asyncio.Redis` 这个**全局模块属性** —— 而 `cache_service` 早就把
    client 缓存下来了，于是 patch 期间被别的测试拿到的就是测试的 FakeRedis，
    表现为大批用例报 `'FakeRedis' object has no attribute 'aclose'`。

    抽成模块级函数后，测试只 patch 这一个点，**不碰 redis 模块本身**，
    污染范围为零。
    """
    import redis.asyncio as redis_async

    from stashbox.backend.common.redis_client import get_redis_pool

    return redis_async.Redis(connection_pool=get_redis_pool())


async def _delete_superseded_audio(article_id: str, previous_audio_url: str) -> None:
    """删掉被新版本覆盖掉的旧音频文件（best-effort）。

    音频 key 现在带内容 hash（app/services/storage/base.py:versioned_audio_key），
    所以「重新生成」= 新的文件名 + 新的 URL，旧文件不会被覆盖而是**变成孤儿**。
    不删的话，每次重跑都在磁盘 / OSS 上留一份音频。

    取舍：删失败只记日志，不影响本次产物 —— 宁可留孤儿文件等 GC，
    不可音频没了文章还在（与 article_purge 的取舍一致）。
    """
    try:
        from stashbox.backend.app.services.storage import get_storage

        storage = get_storage()
        key = storage.key_from_url(previous_audio_url)
        if not key:
            return
        if await storage.exists(key):
            await storage.delete(key)
            log.info("superseded_audio_deleted", article_id=article_id, key=key)
    except Exception as exc:
        log.warning(
            "superseded_audio_delete_failed",
            article_id=article_id,
            url=previous_audio_url[:120],
            error=str(exc),
        )


async def _refund_quota_once(task_id: str, user_id: int, article_id: str) -> None:
    """CP-AGENT-QUOTA-REFUND：退还 1 次配额，Redis SETNX 保证幂等（跨 Arq retry）。

    与 DistillPipeline._refund_quota 同语义，但由任务层调用（agent 路径不经过 pipeline）。
    Redis 不可用时降级为无锁（不阻塞失败流程）。

    ⚠️ **退款失败必须把锁还回去**（2026-10 修）：锁的语义是「这笔已经退过了」，
    但落锁早于退款成功。中间失败（乐观锁冲突、DB 抖一下）会留下一把撒谎的锁，
    之后 24h 内所有 Arq 重试都被挡掉 —— 用户为一次失败的蒸馏照付钱，且零告警。
    详见 `quota_service.release_refund_lock`。

    ⚠️ **退款成功后必须清掉「已扣配额」标记**（2026-10-22 补）：
    退钱与「这篇文章已扣过费」这两件事必须一起变，否则两套状态长期不一致：

    - 标记还在 → 用户手动重试时 `claim_article_quota` 抢不到 → **不再扣费**
      → 钱退了、活也干了，用户白拿一次蒸馏（收入漏损，且无人察觉）；
    - 标记清掉了 → 重试时正常扣费，与「这笔已经退过」的语义一致。

    mock 路径（`ai-service/main.py`）一直是对的（refund 之后紧跟一句
    `clear_article_quota`），只有 agent 路径漏了 —— 同一个动作两套口径。
    """
    from stashbox.backend.common import quota_service

    # 先置 None：落锁那一步自己抛异常时 client 不会被绑定，
    # 下面 except 里引用它会变成 NameError，把退款失败盖成一个更费解的错。
    client = None
    try:
        client = await _make_refund_lock_client()
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
        # 复用落锁时那个 client：让「落锁」和「还锁」走同一份可替换依赖
        await quota_service.release_refund_lock(task_id, client=client)
        log.warning(f"quota_refund_failed task_id={task_id} user_id={user_id} error={exc!s}")
        try:
            from stashbox.backend.common import quota_metrics

            quota_metrics.quota_refund_failed_total.labels(trigger="distill_task").inc()
        except Exception:
            pass
        return

    # 退款成功 → 「已扣配额」标记必须一起清掉（口径与 mock 路径一致）。
    #
    # 放在 try 外面：清标记失败**不能**让整笔退款被当成失败（钱已经退了），
    # 记一条 warn 让人能查即可 —— 标记最坏是让用户下一次重试不扣费（白送一次），
    # 而把退款报错抛出去会让人以为钱没退。
    try:
        await cache_service.clear_article_quota(article_id)
    except Exception as exc:
        log.warning(
            "quota_refund_mark_clear_failed",
            task_id=task_id,
            article_id=article_id,
            user_id=user_id,
            error=str(exc),
        )


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
    title: str | None = None,
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
    # 方案 §1 闭环 1 要求注入 top-5 高分样本。之前这里传的是 `topic=source`
    # （wechat/web/…），而 kind 列只允许 hook/section/outro → 恒返回空集，
    # few-shot 池于是只增不读。改为不按 kind 过滤，取全局最高分的 5 条。
    few_shot_examples = await store.load_few_shots(limit=5)

    initial_state: AgentState = {
        "article_id": article_id,
        "user_id": user_id,
        "url": url,
        "source": source,
        # CP-TITLE-ANNOUNCE（2026-10-26）：标题只给播报用，**不进 prompt**
        # （标题常是标题党措辞，让 LLM 看着写 hook 容易引出正文没有的事实）。
        "title": title,
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
        # 标签候选（tags 表的 name 列表）。LLM 必须逐字从里面选，
        # 订阅推送是按标签名**精确匹配**的（_trigger_subscription_pushes）。
        # 复用 pipeline 的那一份（Redis 缓存 5min），不另造一套。
        "tag_vocabulary": await _load_tag_vocabulary(),
    }

    log.info(
        "agent_run_started",
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        memory_count=len(few_shot_examples),
    )
    final = await _agent_app.ainvoke(initial_state)

    # 蒸馏到此结束（成功或失败）—— 清掉阶段标记。
    # 终态由 articles.status 表达，阶段只描述「进行中」，留着会让客户端
    # 在终态下还显示「正在合成语音…」。
    try:
        from stashbox.backend.common.distill_stage import clear_stage

        await clear_stage(article_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("distill stage 清除失败 article=%s err=%s", article_id, exc)

    # A/B 分组要在落库时写进 distilled_agents（方案 §2.7-D：user_id % 100 < 30
    # → personalized 组，其余 general，intention-to-treat 口径）。
    # 之前只有死代码 pipeline.py 会写这两个字段，生产 8 条记录实测全是 NULL，
    # ab-report 报表因此拿不到任何分组结论。
    ab_group = "personalized" if user_id % 100 < 30 else "general"
    is_personalized = bool(final.get("is_personalized"))
    final = {**final, "ab_group": ab_group, "is_personalized": is_personalized}
    log.info(
        "agent_run_completed",
        task_id=task_id,
        article_id=article_id,
        status=final.get("status"),
        error_kind=final.get("error_kind"),
    )

    # 把 final state 写回 DB
    await _persist_agent_final(task_id, article_id, user_id, final)

    # CP3.7.2 hook 接回活路径（2026-10-02）：生产走 agent 而不是 DistillPipeline，
    # 导致 7 个 hook 全部悬空 —— 评分预测 / 自动重蒸 / 画像更新 / few-shot 入池
    # 一次都没跑过。这里显式补上，产物已落库，hook 挂了只告警。
    await _run_agent_post_hooks(task_id, article_id, user_id, url, raw_content, final)
    return final


async def _run_agent_post_hooks(
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    raw_content: str,
    final: dict,
) -> None:
    """蒸馏成功后触发 CP3.7.2 post-hooks。

    整体包 try/except：hook 失败绝不能把已成功的蒸馏标记成失败。
    """
    try:
        from distill.agent_hook_bridge import build_ctx_from_agent_final, run_post_hooks

        ctx = build_ctx_from_agent_final(
            task_id=task_id,
            article_id=article_id,
            user_id=user_id,
            url=url,
            raw_content=raw_content,
            final=final,
        )
        async with AsyncSessionLocal() as db:
            await run_post_hooks(ctx, db)
    except Exception as exc:
        log.warning(
            "agent_post_hooks_failed_continue",
            task_id=task_id,
            article_id=article_id,
            error=str(exc),
        )


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
            # 旧音频 URL（本次覆盖前的值）—— **只有在真的被新音频替换掉时**
            # 才删旧文件，否则每次重跑都在磁盘 / OSS 上留一份孤儿。
            # key 现在带内容 hash，所以旧 URL 与新 URL 必然不同
            # （见 app/services/storage/base.py:versioned_audio_key）。
            previous_audio_url = da.audio_url
            has_new_audio = bool(final.get("tts_audio_url"))
            if has_new_audio:
                # 音频字段**整体**来自本次尝试（有 URL 才有时长，不留上一轮的）
                da.audio_url = final["tts_audio_url"]
                da.duration_sec = (
                    int(final["tts_duration_sec"])
                    if final.get("tts_duration_sec") is not None
                    else None
                )
            else:
                # **失败/无产出必须清掉上一轮留下的音频指针**（2026-10-21 修）。
                #
                # 这一行是按 article_id 复用同一行的（换音色重生成 / 失败重试都走这里），
                # 所以本次没出音频时行里还留着**上一次成功**的 audio_url 和 duration_sec：
                # status='failed' 却带着 146s 的旧音频地址 —— 状态与产出不同源，
                # 任何按「有 audio_url 就算有产物」判断的调用方都会拿到过期音频。
                #
                # 安卓端不受影响（播放器只在 status==READY && audioUrl!=null 时渲染），
                # 但 API 载荷、admin 列表、音频统计全是错的。
                # 旧音频文件本身仍在 SeaweedFS 里，不删，只是这一行不再指向它。
                da.audio_url = None
                da.duration_sec = None
            # CP-TTS-VOICE：溯源「这段音频是谁念的」。
            # None = 回落全局 indextts_ref_audio，来源不可溯源 —— 保持 NULL，
            # 不要回填一个「看起来对」的音色 ID，那会让统计口径失真。
            if final.get("tts_voice_id"):
                da.tts_voice_id = final["tts_voice_id"]
            # A/B 归因（方案 §2.7-D）。NULL 会被 ab-report 归进 pre_experiment
            # 组，报表看起来像「实验没跑」，实际是压根没写过。
            if final.get("ab_group"):
                da.ab_group = final["ab_group"]
            if final.get("is_personalized") is not None:
                da.is_personalized = bool(final["is_personalized"])

            # ── 主题标签 ────────────────────────────────────────────────
            #
            # ⚠️ 这一列是**订阅推送的唯一输入**：
            # `_trigger_subscription_pushes` 第一行就是 `if not da.tags: return 0`。
            # agent 路径此前**从不写这一列**（且 runner 里 rewrite_tags 硬编码 []）
            # → 生产 `distilled_articles.tags` 实测 25 行全空 →
            # **推送一次都没产生过**（`push_notifications` 实测 0 行）。
            #
            # 运营指定的分类（`articles.manual_tags`，0040）也要读 ——
            # 这一行是「运营录的内容靠订阅分发」能不能成立的关键：
            # 没有它，运营指定的分类会被 LLM 猜的覆盖掉。
            art = await db.get(Article, article_id)
            #
            # 写之前**按 tags 表过滤**：推送是 `Tag.name.in_(da.tags)` 精确匹配，
            # LLM 若输出表里没有的名字（「科技资讯」而不是「科技」），
            # 存进去也只是垃圾 → 查不到 → 推送不出。过滤掉比存着好。
            #
            # ── 运营指定的分类**优先**（2026-10-24 产品拍板）────────────
            #
            # 产品决策：admin 录入的内容「**订阅了分类就可以看到，没订阅就看不到**」
            # ⇒ 这类内容的分发**只认分类**，所以分类必须**可靠**。
            # 而分类原先只有一条来源：LLM 猜的 —— 实测 GitHub 稿的 tags 是**空的**
            # （代码库主题大概率不在 tags 表候选里），于是它**永远进不了任何人的
            # 订阅流** = 死内容（运营录了没人看得到，而且没有任何报错）。
            #
            # 所以运营录入时能**指定**分类（`articles.manual_tags`，0040）：
            # 指定了就**以运营指定的为准**，LLM 猜的作废（运营比 LLM 更清楚
            # 这篇东西该归到哪个分类）；没指定才用 LLM 猜的。
            manual_tags = getattr(art, "manual_tags", None) if art else None
            if manual_tags:
                names = [str(t).strip() for t in manual_tags if str(t).strip()]
                if names:
                    valid = await _filter_known_tag_names(db, names)
                    da.tags = valid
                    if valid:
                        log.info(
                            "agent_persist_tags article=%s tags=%s (manual=运营指定)",
                            article_id,
                            valid,
                        )
                    else:
                        # 运营指定的分类**不在 tags 表里** → 订阅匹配不上 → 内容等于死内容。
                        # 记一条 warning，否则「录了没人看得到」又变成查不到原因。
                        log.warning(
                            "agent_persist_tags_none_known article=%s manual=%s "
                            "(分类不在 tags 表里 → 订阅匹配不上，内容不会有任何订阅者看到)",
                            article_id,
                            names,
                        )
            else:
                raw_tags = final.get("rewrite_tags") or []
                if raw_tags:
                    names = [str(t).strip() for t in raw_tags if str(t).strip()]
                    if names:
                        valid = await _filter_known_tag_names(db, names)
                        da.tags = valid
                        if valid:
                            log.info(
                                "agent_persist_tags article=%s tags=%s (raw=%s)",
                                article_id,
                                valid,
                                names,
                            )
                        else:
                            # 全都不是已知标签 → 记一条，否则「推送没来」又变成查不到原因
                            log.warning(
                                "agent_persist_tags_none_known article=%s raw=%s",
                                article_id,
                                names,
                            )

            # ── 三类再创作内容分区（2026-10-23）────────────────────────
            #
            # 这三类在改写 JSON 里是三个独立字段，但解析时被**合并进 sections**
            # （TTS 整稿念的那一份）。合并之后下游就彻底分不清了 ——
            # 落库只有一列 script_text，App 端也没有分区 UI。
            #
            # 这里把三栏**原样**另存进 content_parts：
            # `script_text` / TTS / few-shot 入池**全部不变**（纯增量）。
            da.content_parts = {
                # ⚠️ 用**合并前**的正文要点（rewrite_body_sections）——
                # rewrite_sections 里已经混进了三类，分不开。
                "sections": list(final.get("rewrite_body_sections") or []),
                "insights": list(final.get("rewrite_insights") or []),
                "related": list(final.get("rewrite_related") or []),
                "follow_ups": list(final.get("rewrite_follow_ups") or []),
                # ── CP-TITLE-ANNOUNCE（2026-10-26）────────────────────
                #
                # script_text 的**第一段现在是标题播报句**（"本期分享的标题是：…"），
                # 所以「首段 = hook」这个旧契约不再成立。把三样东西**明确**交出去，
                # 让下游不必去猜：
                # · hook  = 真正的开场钩子（few-shot 入池 / 详情页导语用这个）
                # · outro = 收束段
                # · intro = 标题播报句（App 知道它是口播，不当正文展示）
                #
                # ⚠️ 存量数据没有这三个键 ⇒ 下游必须容忍缺失（回退旧取法）。
                "hook": str(final.get("rewrite_hook") or ""),
                "outro": str(final.get("rewrite_outro") or ""),
                "intro": str(final.get("title_announcement") or ""),
            }
            log.info(
                "agent_persist_content_parts article=%s sections=%d insights=%d "
                "related=%d follow_ups=%d",
                article_id,
                len(da.content_parts["sections"]),
                len(da.content_parts["insights"]),
                len(da.content_parts["related"]),
                len(da.content_parts["follow_ups"]),
            )

            # 先 flush 让 IntegrityError 在这里暴露（而不是拖到 commit 把事务搞废）
            await db.flush()
            await db.commit()
            # 删掉被覆盖掉的旧音频文件（重跑 → 旧版本成孤儿）。
            # best-effort：删不掉只记日志，不影响本次产物（宁可留孤儿文件
            # 等 GC，不可音频没了文章还在 —— 与 article_purge 的取舍一致）。
            #
            # ⚠️ **只在「真的产出了新音频」时才删**（2026-10-24 实测踩到）：
            #   本次失败时 da.audio_url 被清成 None，而 articles.audio_url
            #   还指着旧文件 —— 那一瞬间是「指针还在、文件没了」→
            #   用户侧立刻 404，且**不可逆**（文件已被删）。
            #
            #   实测：存量重跑第 1 篇 TTS 判据误伤 → 整篇失败 →
            #   旧音频文件被删 → articles.audio_url 指向 404 → Arq 重试
            #   成功前用户是听不到这篇的。
            #
            #   失败时**旧文件必须留着**（这一行不再指向它，但文件在 →
            #   可回滚、可人工恢复）；这正是上面那句取舍的本意。
            if has_new_audio and previous_audio_url and previous_audio_url != da.audio_url:
                await _delete_superseded_audio(article_id, previous_audio_url)
        except Exception as exc:
            log.warning(
                "agent_persist_failed",
                task_id=task_id,
                article_id=article_id,
                error=str(exc),
            )
            await db.rollback()
            return  # 数据没落上就没必要再写埋点了

    # ---- 第二步：失效 content-service 的文章详情缓存（BUG#7，2026-09-30 自测）----
    # 蒸馏是 ai-worker 直写 DB，**不会**经过 content-service，所以
    # `GET /api/v1/articles/{id}` 那份 300s 缓存没人清。结果是：
    #   App 轮询 /status（不走缓存）看到 ready，音频能播；
    #   但用户退出详情页再进来，loadArticle 走的是详情接口，拿到的是
    #   缓存里的 `status=distilling / audio_url=null` ——
    # 一篇 20 分钟前就听完的文章又被显示成「蒸馏中」，还要再轮询 90 秒。
    # 实测：改库让蒸馏完成后，详情仍返回 distilling，/status 同时返回 ready。
    #
    # 放这里而不是 content-service 侧，是因为唯一知道「这篇蒸馏结束了」的地方
    # 就是这个函数；放晚了（埋点之后）会被前面的 early return 跳过。
    #
    # 失败路径同样要清：状态从 distilling 变 failed 一样是陈旧缓存问题。
    # 缓存失效失败不能影响业务（日志里能看到就行）。
    try:
        await cache_service.invalidate_article(article_id)
        await cache_service.invalidate_pending(user_id)
    except Exception as exc:
        log.warning(
            "agent_cache_invalidate_failed",
            task_id=task_id,
            article_id=article_id,
            error=str(exc),
        )

    # ---- 第二步：埋点（独立 session，失败只丢埋点，不影响上面的数据）----
    # 失败 → 带 reason，让 admin dashboard 看到"为什么失败"。
    # 注意：DistilledArticle 没有 metadata 列（见模型定义），错误详情
    # 通过 feedback（DISTILL_FAILED.reason）持久化，不写在该行上。
    #
    # reason **不再手工截断**：feedback.reason 只有 varchar(64)，原先这里截到 200
    # 反而超长 → insert 报错 → 整条埋点被 track() 静默吞掉（只留一行 warning）。
    # 现在由 analytics.track() 统一压到列宽，原文自动进 metadata['reason_full']。
    if status == "failed" and final.get("error"):
        async with AsyncSessionLocal() as fb_db:
            try:
                await track(
                    fb_db,
                    EventName.DISTILL_FAILED,
                    user_id=user_id,
                    article_id=article_id,
                    reason=str(final["error"]),
                )
                await fb_db.commit()
            except Exception as exc:
                log.warning(
                    "agent_persist_feedback_failed",
                    task_id=task_id,
                    error=str(exc),
                )
                await fb_db.rollback()
