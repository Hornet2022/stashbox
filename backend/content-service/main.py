"""
content-service（端口 8102） - 文章 CRUD + 待听/听过/收藏/跳过 + 标签 + D9 回调。

CP1.5：全部走真实 PostgreSQL（articles 表）。
数据隔离：文章按 user_id 归属，非 owner 访问详情/操作返回 403。
软删除：删除走 updated deleted_at（本服务不直接删除，CP1.6 再加）。

CP1.7：D9 端到端 —— 不要求登录态 → 建文章 → 自动触发 ai-service 蒸馏 →
客户端轮询 status / audio-url 拿音频。
"""
import json
import re
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

# content-service 目录名带连字符，不能当包导入，故把自身目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402

from clients.ai_client import get_ai_client  # noqa: E402
from fetchers import (  # noqa: E402
    FetcherError,
    FetcherErrorCode,
    get_fetcher,
    map_fetcher_error,
)
from schemas import (  # noqa: E402
    AddArticleRequest,
    ArticleResponse,
    ArticleStatusResponse,
    AudioUrlResponse,
    ClawBotMessageRequest,
    D9AddRequest,
    D9AddResponse,
    WechatMpMessageRequest,
)

from stashbox.backend.common import cache_service, quota_service
from stashbox.backend.common.auth import create_access_token, require_user, require_user_optional
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import (
    BizException,
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.models import (
    AdminOperationLog,
    Article,
    DistilledArticle,
    Tag,
    TagSubscription,
    User,
)
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.analytics import track, track_simple
from stashbox.backend.common.events import EventName

setup_logging("content-service")
app = FastAPI(title="stashbox-content-service", version="0.3.0")
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)

class InvalidRequest(BizException):
    """参数 / 身份类错误（HTTP 400，业务码按场景传）。"""

    http_status = 400


ANONYMOUS_USER_ID = 0  # 匿名文章归属（v1 §4.3.1 无 device_id 列，本期用 user_id=0 标记）
ANONYMOUS_OPEN_ID = "__anonymous__"
AUDIO_URL_TTL_SEC = 3600
OSS_AUDIO_BASE = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def _new_article_id() -> str:
    return f"art_{uuid.uuid4().hex[:24]}"


def _to_response(a: Article) -> ArticleResponse:
    return ArticleResponse(
        id=a.id,
        url=a.url,
        source=a.source,
        title=a.title,
        owner_id=str(a.user_id),
        status=a.status,
        favorite=a.favorite,
        skip=a.skip,
        created_at=a.created_at.isoformat() if a.created_at else "",
    )


async def _get_owned(article_id: str, user_id: int, db: AsyncSession) -> Article:
    result = await db.execute(select(Article).where(Article.id == article_id))
    art = result.scalar_one_or_none()
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art.user_id != user_id:
        raise Forbidden(message="not the owner of this article")
    return art


async def _get_owned_with_task(
    article_id: str, user_id: int, db: AsyncSession
) -> tuple[Article, DistilledArticle | None]:
    """articles LEFT JOIN distilled_articles（pending 时还没有蒸馏任务）。"""
    row = (
        await db.execute(
            select(Article, DistilledArticle)
            .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
            .where(Article.id == article_id)
        )
    ).first()
    if row is None:
        raise NotFound(message=f"article {article_id} not found")
    art, task = row
    if art.user_id != user_id:
        raise Forbidden(message="not the owner of this article")
    return art, task


def _derive_status(art: Article, task: DistilledArticle | None) -> str:
    """ai-service 只更新 distilled_articles，articles.status 会停在 distilling，故按任务派生。"""
    if task is None:
        return art.status
    if task.status == "done":
        return "ready"
    if task.status == "failed":
        return "failed"
    return art.status


def _validate_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise InvalidRequest(message=f"unsupported url scheme: {url}", code=2001)


# 公众号文本里的 URL：纯文本或 <a href> 包裹，够用即可（不引 lxml / bs4）
_URL_RE = re.compile(r'https?://[^\s<>"\'`]+')
# 消息里 URL 常紧跟中文/英文标点（"看这个 https://x.com/a。"），末尾要 trim
_TRAILING_PUNCT = "。，、；：！？）】》」』…“”‘’.,;:!?)\"'"


def _extract_url(text: str) -> str | None:
    """从公众号文本里抽第一条 URL（末尾标点 trim 掉），没有返回 None。"""
    match = _URL_RE.search(text or "")
    if match is None:
        return None
    url = match.group(0).rstrip(_TRAILING_PUNCT)
    return url or None


def _is_valid_url(url: str) -> bool:
    """URL 合法性：scheme 必须 http/https（顺带挡掉 javascript: 这类 XSS）+ 有 host。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def _ensure_anonymous_user(db: AsyncSession) -> None:
    """匿名哨兵用户（id=0）：articles.user_id 有 FK，匿名文章落库前必须存在该行。"""
    await db.execute(
        insert(User)
        .values(
            id=ANONYMOUS_USER_ID,
            open_id=ANONYMOUS_OPEN_ID,
            nickname="anonymous",
            tier="free",
            monthly_quota=0,
        )
        .on_conflict_do_nothing()
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "content-service"}


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _to_raw_content(result) -> dict:
    """FetchResult → JSONB-ready dict。

    asdict 只重建 dict/list/tuple，datetime 会原样保留（不会自动转 ISO 字符串），
    直接写 JSONB 会在序列化时炸，故再走一次 json 归一化。
    """
    return json.loads(json.dumps(asdict(result), default=_json_default))


async def _create_article(
    url: str, user_id: int, source: str, title: str | None, db: AsyncSession,
    raw_content: dict | None = None,  # 默认为 None：其他调用点行为不变
) -> Article:
    art = Article(
        id=_new_article_id(),
        user_id=user_id,
        url=url,
        source=source,
        title=title,
        status="pending",
        raw_content=raw_content,  # FetchResult 全字段（JSONB，ai-service 蒸馏输入）
        favorite=False,
        skip=False,
    )
    db.add(art)
    await db.commit()
    await db.refresh(art)
    await cache_service.invalidate_pending(user_id)  # 待听列表缓存失效
    return art


@app.post("/api/v1/articles/add", response_model=ArticleResponse)
async def add_article(
    req: AddArticleRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _create_article(req.url, int(user["sub"]), req.source, None, db)
    return _to_response(art)


@app.post("/api/v1/articles")
async def submit_article(
    req: AddArticleRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """提交链接（v1 §3.1）：扣 1 次配额 → 建文章 → 配额/待听缓存失效。

    扣减走 quota_service 乐观锁（含 Redis Lua 原子失效），配额用尽抛 3001。
    """
    uid = int(user["sub"])
    quota = await quota_service.consume(db, uid)  # 用尽抛 QuotaExceededError(3001)
    art = await _create_article(req.url, uid, req.source, None, db)
    await cache_service.mark_article_quota(art.id)  # 打标：该文章已扣过配额
    # CP6.2.1 埋点：article_submit
    await track_simple(db, EventName.ARTICLE_SUBMIT, uid, art.id)
    return {
        "article_id": art.id,
        "url": art.url,
        "status": art.status,
        "quota_used": quota["quota_used"],
        "monthly_quota": quota["monthly_quota"],
        "remaining": quota["monthly_quota"] - quota["quota_used"],
    }


@app.get("/api/v1/articles/pending")
async def list_pending(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    uid = int(user["sub"])
    cached = await cache_service.get_pending(uid)
    if cached is not None:
        return {"articles": cached, "count": len(cached), "cached": True}

    result = await db.execute(
        select(Article).where(
            Article.user_id == uid,
            Article.status.in_(["pending", "distilling", "ready"]),
            Article.skip.is_(False),
            Article.deleted_at.is_(None),
        )
    )
    items = result.scalars().all()
    items = [_to_response(a).model_dump() for a in items]
    await cache_service.set_pending(uid, items)  # 回填（ttl 60s）
    return {"articles": items, "count": len(items), "cached": False}


@app.get("/api/v1/articles/listened")
async def list_listened(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Article).where(
            Article.user_id == int(user["sub"]),
            Article.status == "listened",
            Article.deleted_at.is_(None),
        )
    )
    items = result.scalars().all()
    items = [_to_response(a) for a in items]
    return {"articles": items, "count": len(items)}


@app.get("/api/v1/articles/{article_id}", response_model=ArticleResponse)
async def get_article(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    cached = await cache_service.get_article(article_id)
    if cached:
        return cached

    art = await _get_owned(article_id, int(user["sub"]), db)
    payload = _to_response(art).model_dump()
    await cache_service.set_article(article_id, payload)  # ttl 300s
    return payload


@app.post("/api/v1/articles/{article_id}/mark-listened")
async def mark_listened(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _get_owned(article_id, int(user["sub"]), db)
    art.status = "listened"
    await db.commit()
    # CP6.2.1 埋点：audio_complete
    await track_simple(db, EventName.AUDIO_COMPLETE, int(user["sub"]), article_id)
    return {"id": article_id, "status": "listened"}


@app.post("/api/v1/articles/{article_id}/favorite")
async def favorite(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _get_owned(article_id, int(user["sub"]), db)
    art.favorite = True
    await db.commit()
    return {"id": article_id, "favorite": True}


@app.post("/api/v1/articles/{article_id}/skip")
async def skip(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _get_owned(article_id, int(user["sub"]), db)
    art.skip = True
    await db.commit()
    return {"id": article_id, "skip": True}


@app.post("/api/v1/callback/d9-add-article", response_model=D9AddResponse)
async def d9_add_article(
    req: D9AddRequest,
    user: dict | None = Depends(require_user_optional),
    device_id: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
):
    """D9 入口（v1 §3.5）：微信「更多打开方式」→ 听匣，不要求登录态。

    1. 解析 caller：已登录用 user_id，未登录用 device_id（两者都没有 → 4001）
    2. 配额预扣（仅已登录；匿名不计费）
    3. 建 articles 行（status=pending）
    4. 触发 ai-service 蒸馏（失败只 log，不影响 D9 返回）
    """
    if user is None and not device_id:
        raise InvalidRequest(message="device_id required for anonymous D9", code=4001)
    _validate_url(req.url)

    if user is not None:
        uid = int(user["sub"])
        await quota_service.consume(db, uid)  # 用尽抛 QuotaExceededError(3001)
    else:
        uid = ANONYMOUS_USER_ID
        await _ensure_anonymous_user(db)

    art = await _create_article(req.url, uid, req.source, req.title, db)
    # 打标：该文章已扣过配额（匿名不计费也算），避免 ai-service 蒸馏时重复扣
    await cache_service.mark_article_quota(art.id)

    task = await get_ai_client().trigger_distill(art.id, auth_token=create_access_token(str(uid)))
    return D9AddResponse(
        article_id=art.id,
        task_id=(task or {}).get("task_id"),
        status="distilling" if task else art.status,
        device_id=device_id,
    )


@app.get("/api/v1/articles/{article_id}/status", response_model=ArticleStatusResponse)
async def article_status(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """文章 + 蒸馏任务聚合状态（v1 §11.4 CP1.7）：客户端轮询这个端点等 ready。"""
    art, task = await _get_owned_with_task(article_id, int(user["sub"]), db)
    return ArticleStatusResponse(
        article_id=art.id,
        status=_derive_status(art, task),
        task_id=task.id if task else None,
        task_status=task.status if task else None,
        error=art.error,
        audio_url=task.audio_url if task else None,
        audio_duration_sec=task.duration_sec if task else None,
        tags=task.tags if task else None,
        quality_score=task.quality_score if task else None,
        created_at=art.created_at.isoformat() if art.created_at else "",
        updated_at=art.updated_at.isoformat() if art.updated_at else "",
    )


@app.get("/api/v1/articles/{article_id}/audio-url", response_model=AudioUrlResponse)
async def article_audio_url(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """取音频播放地址：仅 status=ready 可用，其余 404。

    OSS 签名本期 mock（CP1.8+ 接真签名，依赖阿里云 RAM 配置）。
    """
    art, task = await _get_owned_with_task(article_id, int(user["sub"]), db)
    if _derive_status(art, task) != "ready":
        raise NotFound(message=f"audio not ready for article {article_id}")

    expires_ts = int(time.time()) + AUDIO_URL_TTL_SEC
    base = (task.audio_url if task else None) or f"{OSS_AUDIO_BASE}/{art.id}.m4a"
    # CP6.2.1 埋点：audio_play_start
    await track_simple(db, EventName.AUDIO_PLAY_START, int(user["sub"]), article_id)
    return AudioUrlResponse(
        article_id=art.id,
        audio_url=f"{base}?Expires={expires_ts}&OSSAccessKeyId=mock&Signature=mock",
        expires_at=datetime.fromtimestamp(expires_ts, timezone.utc).isoformat(),
        duration_sec=(task.duration_sec if task else None) or 0,
    )


@app.post("/api/v1/callback/clawbot-message")
async def clawbot_message(
    req: ClawBotMessageRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """ClawBot 入口（mock）：接收消息，解析出 URL 则建文章。"""
    art = None
    if "http" in req.text:
        art = await _create_article(req.text, int(user["sub"]), "clawbot", None, db)
    return {"received": True, "article": _to_response(art) if art else None}


@app.post("/api/v1/callback/wechat-mp-message")
async def wechat_mp_message(
    req: WechatMpMessageRequest, db: AsyncSession = Depends(get_db)
):
    """微信公众号服务号回调（v1 §11.2 CP2.5）。

    接收用户发给服务号的 URL → 路由 fetcher 抓正文 → 建文章 → 触发蒸馏。

    - 不需要 JWT（公众号回调，公众号已认证用户身份）
    - 不扣配额（匿名入口，等客户端登录后再扣）
    - 失败抛 BizException(2001=URL 不支持 / 2002=抓取失败)
    """
    url = _extract_url(req.text)
    if url is None:
        raise InvalidRequest(message="no url found", code=2001)
    if not _is_valid_url(url):
        raise InvalidRequest(message=f"url invalid: {url}", code=2001)

    fetcher = get_fetcher(url)
    if fetcher is None:  # 理论不会发生（generic_url 兜底），留着防回归
        raise InvalidRequest(message="no fetcher matched", code=2001)

    try:
        result = await fetcher.fetch(url)
    except FetcherError as exc:
        # CP6.2.2.2a: ARTICLE_CAPTURE_FAILED / ARTICLE_UNSUPPORTED 埋点
        if exc.code == FetcherErrorCode.UNSUPPORTED:
            await track(db, EventName.ARTICLE_UNSUPPORTED,
                        user_id=ANONYMOUS_USER_ID, article_id="n/a",
                        metadata={"error": exc.message})
        else:
            await track(db, EventName.ARTICLE_CAPTURE_FAILED,
                        user_id=ANONYMOUS_USER_ID, article_id="n/a",
                        metadata={"error": exc.code.value if hasattr(exc.code, 'value') else str(exc.code)})
        raise map_fetcher_error(exc) from exc

    await _ensure_anonymous_user(db)
    art = await _create_article(
        url, ANONYMOUS_USER_ID, "wechat_mp", result.title or None, db,
        raw_content=_to_raw_content(result),  # FetchResult → JSONB，免二次抓取
    )
    task = await get_ai_client().trigger_distill(
        art.id, auth_token=create_access_token(str(ANONYMOUS_USER_ID))
    )
    return {
        "received": True,
        "article_id": art.id,
        "task_id": (task or {}).get("task_id"),
        "title": art.title,
        "source": art.source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_text_length": len(result.content_text),
        "has_media": len(result.media_urls) > 0,
    }


@app.get("/api/v1/tags")
async def list_tags(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    # CP5.3a：从 DB 读标签（不再用 mock）
    result = await db.execute(select(Tag).order_by(Tag.category, Tag.name))
    tags = result.scalars().all()
    return {
        "tags": [
            {
                "id": tag.slug,  # 用 slug 作为 id（与 mock 兼容）
                "name": tag.name,
                "category": tag.category,
            }
            for tag in tags
        ]
    }


class TagCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=64)
    category: str = Field("subject", max_length=32)


@app.post("/api/v1/tags")
async def create_tag(
    req: TagCreateRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """admin/operator 创自定义标签（CP5.3b）。"""
    existing = await db.scalar(select(Tag).where(Tag.slug == req.slug))
    if existing:
        raise HTTPException(status_code=409, detail=f"tag slug 已存在: {req.slug}")

    tag = Tag(
        slug=req.slug,
        name=req.name,
        category=req.category,
        is_system=False,
        creator_id=user["id"],
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)

    await track_simple(db, EventName.TAG_CREATE, user_id=user["id"],
                       properties={"tag_slug": tag.slug, "category": tag.category})

    return {
        "id": tag.slug,
        "name": tag.name,
        "category": tag.category,
    }


@app.post("/api/v1/tags/{tag_id_or_slug}/subscribe")
async def subscribe_tag(
    tag_id_or_slug: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """用户订阅标签（CP5.3b）。tag_id_or_slug 接受 slug 或数字 id。"""
    if tag_id_or_slug.isdigit():
        tag = await db.get(Tag, int(tag_id_or_slug))
    else:
        tag = await db.scalar(select(Tag).where(Tag.slug == tag_id_or_slug))
    if not tag:
        raise HTTPException(status_code=404, detail=f"tag 不存在: {tag_id_or_slug}")

    existing = await db.scalar(
        select(TagSubscription).where(
            TagSubscription.user_id == user["id"],
            TagSubscription.tag_id == tag.id,
        )
    )
    if existing:
        return {"ok": True, "already_subscribed": True}

    sub = TagSubscription(user_id=user["id"], tag_id=tag.id)
    db.add(sub)
    await db.commit()

    await track_simple(db, EventName.TAG_SUBSCRIBE, user_id=user["id"],
                       properties={"tag_slug": tag.slug})

    return {"ok": True, "tag_id": tag.id, "tag_slug": tag.slug}


@app.post("/api/v1/tags/{tag_id_or_slug}/unsubscribe")
async def unsubscribe_tag(
    tag_id_or_slug: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """用户取消订阅标签（CP5.3b）。"""
    if tag_id_or_slug.isdigit():
        tag = await db.get(Tag, int(tag_id_or_slug))
    else:
        tag = await db.scalar(select(Tag).where(Tag.slug == tag_id_or_slug))
    if not tag:
        raise HTTPException(status_code=404, detail=f"tag 不存在: {tag_id_or_slug}")

    result = await db.execute(
        delete(TagSubscription).where(
            TagSubscription.user_id == user["id"],
            TagSubscription.tag_id == tag.id,
        )
    )
    await db.commit()

    if result.rowcount == 0:
        return {"ok": True, "already_unsubscribed": True}

    await track_simple(db, EventName.TAG_UNSUBSCRIBE, user_id=user["id"],
                       properties={"tag_slug": tag.slug})

    return {"ok": True, "tag_id": tag.id, "tag_slug": tag.slug}


# TODO: tag_filter 埋点（CP5.3b）—— v1 §11.5 没明确 filter 触发位置，GET /api/v1/articles ?tag=xxx 是 CP5.3 后续工作，留在 [known issues] 报备


class AdminActionRequest(BaseModel):
    """admin 写操作统一 body：reason 必填（审计留痕）。"""

    reason: str


# ---------------------------------------------------------------------------
# CP3.6-A3 admin 其他端点（v1 §3.6）
# ---------------------------------------------------------------------------
@app.post("/api/v1/admin/articles/{article_id}/force-retry")
async def admin_force_retry(
    article_id: str,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 强制重试蒸馏。

    行为：
      1. reason ≥5 字符校验（commit 前，失败不落库）
      2. 校验 article 存在（不存在 404）
      3. 状态置 pending（article 表无 retry_count 列，以 status=pending 表达"待重试"，
         由后续 ai-service / worker 重新蒸馏）
      4. 同事务写 admin_operation_logs 一条（A1 已建表）
      5. 提交后触发 ai-service 蒸馏；不可达时仅置 pending，由 worker 自动重试
    失败回滚事务。
    """
    if len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="reason 至少 5 个字符")

    art = await db.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail=f"article {article_id} not found")

    art.status = "pending"

    log_row = AdminOperationLog(
        admin_id=int(user["sub"]),
        admin_tier=user.get("tier", "unknown"),
        action="force_retry",
        target_type="article",
        target_id=article_id,
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/articles/{article_id}/force-retry",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 触发蒸馏（ai-service 不可达返回 None，不破请求；status=pending 让 worker 自动重试）
    queued = await get_ai_client().trigger_distill(
        article_id, auth_token=create_access_token(str(art.user_id))
    )
    return {
        "article_id": article_id,
        "status": "pending",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "distill_triggered": queued is not None,
    }


@app.post("/api/v1/admin/audio/{audio_id}/invalidate")
async def admin_audio_invalidate(
    audio_id: str,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 音频文件作废。

    本仓库无独立 audio_files 表，音频实体即 distilled_articles（含 audio_url）。
    行为：
      1. reason ≥5 字符校验
      2. 校验 distilled_article 存在（不存在 404）
      3. 状态置 invalidated
      4. 同事务写 admin_operation_logs 一条
    幂等：重复调用保持 invalidated 状态，仍记录操作日志。
    失败回滚事务。
    """
    if len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="reason 至少 5 个字符")

    audio = await db.get(DistilledArticle, audio_id)
    if audio is None:
        raise HTTPException(status_code=404, detail=f"audio {audio_id} not found")

    audio.status = "invalidated"

    log_row = AdminOperationLog(
        admin_id=int(user["sub"]),
        admin_tier=user.get("tier", "unknown"),
        action="audio_invalidate",
        target_type="audio",
        target_id=audio_id,
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/audio/{audio_id}/invalidate",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {"audio_id": audio_id, "status": "invalidated"}


@app.get("/api/v1/admin/audit-log")
async def admin_audit_log(
    page: int = 1,
    size: int = 20,
    actor_id: str | None = None,
    action_type: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 审计日志查询（读 admin_operation_logs，CP3.6-A1）。

    查询参数：page / size / actor_id / action_type / from / to
    排序：created_at DESC；过滤：actor_id exact + action_type exact + 时间范围。
    """
    page = max(page, 1)
    size = max(min(size, 100), 1)
    offset = (page - 1) * size

    query = select(AdminOperationLog)
    if actor_id is not None:
        try:
            query = query.where(AdminOperationLog.admin_id == int(actor_id))
        except ValueError:
            pass  # 非数字 actor_id 不匹配任何行，返回空
    if action_type is not None:
        query = query.where(AdminOperationLog.action == action_type)
    if from_ is not None:
        query = query.where(AdminOperationLog.created_at >= from_)
    if to is not None:
        query = query.where(AdminOperationLog.created_at <= to)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(AdminOperationLog.created_at.desc())
            .offset(offset)
            .limit(size)
        )
    ).scalars().all()

    items = [
        {
            "id": r.id,
            "actor_id": r.admin_id,
            "action_type": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "payload": r.request_body,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]
    return {"total": total or 0, "items": items}


async def _safe_revenue(db: AsyncSession) -> float:
    """本月已支付订单金额合计（revenue）。

    orders 表在部分部署可能不存在（无独立 migration 约束），缺表/缺列时返回 0
    而非让 stats 端点整体 500。
    """
    try:
        val = await db.scalar(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM orders "
                "WHERE status = 'paid' "
                "AND date_trunc('month', created_at) = date_trunc('month', now())"
            )
        )
        return float(val or 0)
    except Exception:
        return 0.0


@app.get("/api/v1/admin/stats")
async def admin_stats(user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)):
    total_users = await db.scalar(select(func.count()).select_from(User))
    total_articles = await db.scalar(select(func.count()).select_from(Article))
    pending = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.status == "pending", Article.deleted_at.is_(None))
    )
    listened = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.status == "listened", Article.deleted_at.is_(None))
    )

    # CP3.6-A3 新增字段（不破坏现有结构，仅加字段）
    # failed_distillations_24h：articles 近 24h 失败
    failed_24h = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(
            Article.status == "failed",
            Article.created_at > (func.now() - timedelta(days=1)),
        )
    )
    # active_audio_files：distilled_articles done 且有 audio_url（映射 audio_files ready）
    active_audio = await db.scalar(
        select(func.count())
        .select_from(DistilledArticle)
        .where(
            DistilledArticle.status == "done",
            DistilledArticle.audio_url.isnot(None),
        )
    )
    # revenue：orders 本月已支付（表可能缺失 → 0）
    revenue = await _safe_revenue(db)

    return {
        "total_users": total_users or 0,
        "total_articles": total_articles or 0,
        "pending": pending or 0,
        "listened": listened or 0,
        "revenue": revenue,
        "active_audio_files": active_audio or 0,
        "failed_distillations_24h": failed_24h or 0,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
