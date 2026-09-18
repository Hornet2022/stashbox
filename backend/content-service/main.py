"""
content-service（端口 8102） - 文章 CRUD + 待听/听过/收藏/跳过 + 标签 + D9 回调。

CP1.5：全部走真实 PostgreSQL（articles 表）。
数据隔离：文章按 user_id 归属，非 owner 访问详情/操作返回 403。
软删除：删除走 updated deleted_at（本服务不直接删除，CP1.6 再加）。

CP1.7：D9 端到端 —— 不要求登录态 → 建文章 → 自动触发 ai-service 蒸馏 →
客户端轮询 status / audio-url 拿音频。
"""
import csv
import io
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
from fastapi.responses import StreamingResponse
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
from stashbox.backend.common.database import AsyncSessionLocal, get_db
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
    Feedback,
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


# ---------------------------------------------------------------------------
# CP5.2 用户端 distill 失败重试（v1 §11.5）
# ---------------------------------------------------------------------------


async def push_retry_message(user_id: int, article_id: str) -> None:
    """v1 §11.5 CP5.2 '换源重试' 推送卡片。

    复用 CP5.4b push 队列（write_notification），不接极光推送（红线）。
    本期卡片类型 = retry_card，data 字段含 article_id + suggested_alternative。
    注意：PushNotification 模型无 type/data 字段，简化 title+body 直接展示。
    """
    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models.push_notification import PushNotification

    async with AsyncSessionLocal() as session:
        notif = PushNotification(
            user_id=user_id,
            article_id=article_id,
            title="换个来源重试？",
            body="这篇原文被拒收，要不要换一个源？",
        )
        session.add(notif)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            # 推送失败不破请求
            pass


@app.post("/api/v1/articles/{article_id}/retry")
async def user_retry_distill(
    article_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """v1 §11.5 CP5.2 用户端蒸馏失败重试。

    行为：
      1. 校验 article 存在（不存在 404）
      2. 校验 article 属于当前 user（user_id 不匹配 403）
      3. 校验状态 = failed（其他状态 409 conflict）
      4. 状态置 pending，retry_count += 1
      5. 触发 ai-service 蒸馏（不可达时仅置 pending，worker 自动重试）
      6. 触发推送"换源重试"卡片（CP5.4b push 队列已有，写消息）
    失败回滚事务。
    """
    art = await db.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail=f"article {article_id} not found")

    if art.user_id != int(user["sub"]):
        raise HTTPException(status_code=403, detail="not your article")

    if art.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"article status is {art.status}, only 'failed' can retry",
        )

    art.status = "pending"
    art.retry_count = (art.retry_count or 0) + 1

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 触发蒸馏
    queued = await get_ai_client().trigger_distill(
        article_id, auth_token=create_access_token(str(art.user_id))
    )

    # 写推送"换源重试"卡片（CP5.4b push 队列）
    await push_retry_message(art.user_id, article_id)

    # CP5.2 埋点
    await track_simple(db, EventName.ARTICLE_RETRY_REQUESTED, int(user["sub"]), article_id)

    return {
        "article_id": article_id,
        "status": "pending",
        "retry_count": art.retry_count,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "distill_triggered": queued is not None,
    }


# ---------------------------------------------------------------------------
# CP5.5 文章反馈闭环（v1 §3.1 / §4.3.4）：4 端点写 feedback 表
# ---------------------------------------------------------------------------
SKIP_REASONS = ("too_long", "boring", "low_quality", "other")


class SkipRequest(BaseModel):
    """skip 原因（v1 §3.1）：4 选 1。字段缺失/非法走业务 400（非 422）。"""

    reason: str | None = None


class ListenCompleteRequest(BaseModel):
    """听完上报：duration_sec 可选（客户端播放时长，用于断点续听分析）。"""

    duration_sec: int | None = None


class RateRequest(BaseModel):
    """评分：1-5 星 + 可选评论。越界走业务 400（非 422）。"""

    rating: int | None = None
    comment: str | None = None


async def _write_feedback(
    db: AsyncSession,
    user_id: int,
    article_id: str,
    type_: str,
    *,
    rating: int | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> Feedback:
    """写 feedback 行（v1 §4.3.4）。

    只 add 不 commit —— 调用方把 feedback 写和 article 字段更新放同一事务提交。
    """
    fb = Feedback(
        user_id=user_id,
        article_id=article_id,
        type=type_,
        rating=rating,
        reason=reason,
        metadata_=metadata if metadata is not None else {},
    )
    db.add(fb)
    return fb


@app.post("/api/v1/articles/{article_id}/favorite")
async def favorite(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """收藏（v1 §3.1）：articles.favorite=True + feedback(type=favorite)，同一事务。"""
    uid = int(user["sub"])
    art = await _get_owned(article_id, uid, db)
    art.favorite = True
    fb = await _write_feedback(db, uid, article_id, "favorite")
    await db.commit()
    await db.refresh(fb)  # commit 后 id/created_at 需回读（expire_on_commit）
    return {"id": article_id, "favorite": True, "feedback_id": fb.id}


@app.post("/api/v1/articles/{article_id}/skip")
async def skip(
    article_id: str,
    req: SkipRequest = SkipRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """跳过（v1 §3.1）：articles.skip=True + feedback(type=skip, reason=...)，同一事务。"""
    if not req.reason:
        raise InvalidRequest(message="reason is required", code=4001)
    if req.reason not in SKIP_REASONS:
        raise InvalidRequest(message=f"invalid reason: {req.reason}", code=4001)

    uid = int(user["sub"])
    art = await _get_owned(article_id, uid, db)
    art.skip = True
    fb = await _write_feedback(db, uid, article_id, "skip", reason=req.reason)
    await db.commit()
    await db.refresh(fb)
    return {"id": article_id, "skip": True, "feedback_id": fb.id}


@app.post("/api/v1/articles/{article_id}/listen-complete")
async def listen_complete(
    article_id: str,
    req: ListenCompleteRequest = ListenCompleteRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """听完上报（v1 §3.1 mark-listened 的反馈闭环版）：写 feedback(type=listen_complete)。

    articles 表无 listened_at 列（v1 §4.3.1 未建，本期红线不动 alembic），
    故 listened_at 取 feedback.created_at —— 同一事务里 DB 侧 NOW()，语义等价。
    """
    uid = int(user["sub"])
    await _get_owned(article_id, uid, db)
    meta = {"duration_sec": req.duration_sec} if req.duration_sec is not None else {}
    fb = await _write_feedback(db, uid, article_id, "listen_complete", metadata=meta)
    await db.commit()
    await db.refresh(fb)
    return {
        "id": article_id,
        "listened_at": fb.created_at.isoformat() if fb.created_at else None,
        "feedback_id": fb.id,
    }


@app.post("/api/v1/articles/{article_id}/rate")
async def rate(
    article_id: str,
    req: RateRequest = RateRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """评分（1-5 星）：写 feedback(type=rate, rating=..., metadata={comment})。

    刻意**不**改 articles.quality_score —— 避免与推荐算法循环依赖，留 CP5.6 离线计算。
    """
    if req.rating is None:
        raise InvalidRequest(message="rating is required", code=4001)
    if not 1 <= req.rating <= 5:
        raise InvalidRequest(message="rating must be between 1 and 5", code=4001)

    uid = int(user["sub"])
    await _get_owned(article_id, uid, db)
    meta = {"comment": req.comment} if req.comment else {}
    fb = await _write_feedback(
        db, uid, article_id, "rate", rating=req.rating, metadata=meta
    )
    await db.commit()
    await db.refresh(fb)
    return {"id": article_id, "rating": req.rating, "feedback_id": fb.id}


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


# ---------------------------------------------------------------------------
# CP5.6 admin CSV 数据导出（5 端点，v1 §11.5）
#
# 实现约束（本任务红线）：
#   - 标准库 csv + io.StringIO 生成，不引第三方（pandas / openpyxl）
#   - StreamingResponse 逐批下发，避免大表一次性进内存
#   - 不接 OSS / S3、不加 gzip、不加 limit（admin 全量）
#   - 鉴权统一 require_admin_or_operator；每次导出写一条 admin_operation_logs
# ---------------------------------------------------------------------------
_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
_EXPORT_LOG_ACTION = "ADMIN_EXPORT"


def _csv_filename(name: str) -> str:
    """users -> users-2026-09-17.csv（UTC 日期）。"""
    return f"{name}-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"


def _csv_cell(value):
    """单元格归一化：None -> ""，datetime -> ISO8601，dict/list -> JSON 文本。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _csv_chunk(rows: list[list]) -> str:
    """把一批行渲染成 CSV 文本（行尾 CRLF，符合 RFC 4180）。"""
    buf = io.StringIO()
    csv.writer(buf).writerows([[_csv_cell(c) for c in row] for row in rows])
    return buf.getvalue()


def _stream_csv(filename: str, header: list[str], fetch_rows=None) -> StreamingResponse:
    """组 StreamingResponse：BOM + header + 逐行下发。

    ``fetch_rows(session)`` 返回 async 迭代器；用**独立 session**（而非请求级
    ``Depends(get_db)``）在生成器内执行，避免请求级 session 在响应体流式发送
    期间被依赖注入提前 close。``fetch_rows=None`` 时只回 header（表缺失降级）。
    """
    async def _gen():
        yield "\ufeff"  # UTF-8 BOM：Excel 直接打开中文列名/内容不乱码
        yield _csv_chunk([header])
        if fetch_rows is None:
            return
        async with AsyncSessionLocal() as session:
            async for row in fetch_rows(session):
                yield _csv_chunk([row])

    return StreamingResponse(
        _gen(),
        media_type=_CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _write_export_log(
    db: AsyncSession, user: dict, path: str, filename: str, row_count: int
) -> None:
    """每次导出写一条 admin_operation_logs（响应开始前提交，客户端中断也留痕）。"""
    db.add(
        AdminOperationLog(
            admin_id=int(user["sub"]),
            admin_tier=user.get("tier", "unknown"),
            action=_EXPORT_LOG_ACTION,
            target_type="export",
            target_id=filename,
            reason=f"admin CSV 导出 {filename}",
            method="GET",
            path=path,
            request_body={"filename": filename, "row_count": row_count},
            response_status=200,
        )
    )
    await db.commit()


async def _table_exists(db: AsyncSession, name: str) -> bool:
    """表是否存在。v1 §4 部分表尚未落 migration，缺失时导出降级为 header-only。"""
    return bool(
        await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name)"
            ),
            {"name": name},
        )
    )


@app.get("/api/v1/admin/export/users.csv")
async def admin_export_users_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 users 全量（v1 §4.2.1 字段 + 月配额 / 已用配额）。

    display_name / role 是 v1 §3.6 admin web 的字段名（对应 users.nickname / users.tier）；
    last_active_at 本仓库 users 表未建该列（v1 §4.2.1 未列），列位保留但恒为空，
    以免 admin web 表头随实现漂移。
    """
    path = "/api/v1/admin/export/users.csv"
    filename = _csv_filename("users")
    header = [
        "id", "email", "display_name", "role", "tier", "status",
        "monthly_quota", "used_quota", "last_active_at", "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(User)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        result = await session.stream(select(User).order_by(User.id))
        async for u in result.scalars():
            yield [
                u.id, u.email, u.nickname, u.tier, u.tier, "active",
                u.monthly_quota, u.quota_used, None, u.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@app.get("/api/v1/admin/export/articles.csv")
async def admin_export_articles_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 articles 全量 + distilled_articles 标签 / 质量分（LEFT JOIN）。

    listened_at：articles 表无该列（v1 §4.3.1 未建），沿用 CP5.5 口径取
    feedback(type='listen_complete') 的 created_at 最大值。
    """
    path = "/api/v1/admin/export/articles.csv"
    filename = _csv_filename("articles")
    header = [
        "id", "user_id", "title", "source", "url", "status",
        "tags", "quality_score", "listened_at", "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(Article)) or 0
    await _write_export_log(db, user, path, filename, total)

    listened_at = (
        select(func.max(Feedback.created_at))
        .where(Feedback.article_id == Article.id, Feedback.type == "listen_complete")
        .correlate(Article)
        .scalar_subquery()
    )

    async def fetch(session: AsyncSession):
        stmt = (
            select(Article, DistilledArticle.tags, DistilledArticle.quality_score, listened_at)
            .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
            .order_by(Article.created_at, Article.id)
        )
        result = await session.stream(stmt)
        async for row in result:
            art, tags, score, listened = row
            yield [
                art.id, art.user_id, art.title, art.source, art.url, art.status,
                "|".join(str(t) for t in tags) if tags else "",
                score, listened, art.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@app.get("/api/v1/admin/export/feedback.csv")
async def admin_export_feedback_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 feedback 全量（v1 §4.3.4 全字段）。"""
    path = "/api/v1/admin/export/feedback.csv"
    filename = _csv_filename("feedback")
    header = ["id", "user_id", "article_id", "type", "rating", "reason", "metadata", "created_at"]
    total = await db.scalar(select(func.count()).select_from(Feedback)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        result = await session.stream(select(Feedback).order_by(Feedback.id))
        async for f in result.scalars():
            yield [
                f.id, f.user_id, f.article_id, f.type,
                f.rating, f.reason, f.metadata_, f.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@app.get("/api/v1/admin/export/audit-log.csv")
async def admin_export_audit_log_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 admin_operation_logs 全量（v1 §3.6 5 原则 2 审计留痕）。

    排序与 GET /api/v1/admin/audit-log 一致（created_at DESC）。
    row_count 在写本次导出日志**之前**统计，故不含本次这条。
    """
    path = "/api/v1/admin/export/audit-log.csv"
    filename = _csv_filename("audit-log")
    header = [
        "id", "actor_id", "action_type", "target_type", "target_id", "payload", "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(AdminOperationLog)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        stmt = select(AdminOperationLog).order_by(
            AdminOperationLog.created_at.desc(), AdminOperationLog.id.desc()
        )
        result = await session.stream(stmt)
        async for r in result.scalars():
            yield [
                r.id, r.admin_id, r.action, r.target_type,
                r.target_id, r.request_body, r.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@app.get("/api/v1/admin/export/subscriptions.csv")
async def admin_export_subscriptions_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 subscriptions 全量（v1 §4.6.1）。

    [known issue] 本仓库 subscriptions 表尚未实现（无 ORM 模型 / 无 migration，
    本地 alembic 停在 0004），故表缺失时降级为「只回 header」的合法 CSV 而非 500
    —— 与 admin stats 对同样未实现的 orders 表的处理口径一致（见 _safe_revenue）。
    表落地后本端点无需改代码即自动生效。
    """
    path = "/api/v1/admin/export/subscriptions.csv"
    filename = _csv_filename("subscriptions")
    header = ["id", "user_id", "tier", "started_at", "expires_at", "status", "auto_renew"]

    if not await _table_exists(db, "subscriptions"):
        await _write_export_log(db, user, path, filename, 0)
        return _stream_csv(filename, header)

    total = await db.scalar(text("SELECT count(*) FROM subscriptions")) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        # v1 §4.6.1 建表列名是 start_at / expire_at，导出表头按 admin web 契约用 started_at / expires_at
        result = await session.stream(
            text(
                "SELECT id, user_id, tier, start_at AS started_at, expire_at AS expires_at, "
                "status, auto_renew FROM subscriptions ORDER BY id"
            )
        )
        async for r in result:
            yield [
                r.id, r.user_id, r.tier, r.started_at,
                r.expires_at, r.status, r.auto_renew,
            ]

    return _stream_csv(filename, header, fetch)


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
