"""
user-service（端口 8101） - 登录 + 配额 + 订阅。

CP1.5：wechat-login / user 走真实 PostgreSQL（users 表）；
quota / subscription/plans 仍为 mock（配额扣减事务在 CP1.6）。
鉴权：JWT 的 sub = users.id（整数），下游据此校验归属。
"""

import asyncio
import bcrypt
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stashbox.backend.common.auth import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    require_user,
)
from stashbox.backend.common.config import settings
from stashbox.backend.common import cache_service
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import (
    BizException,
    NotFound,
    Unauthorized,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.auth_admin import require_admin, require_admin_or_operator
from stashbox.backend.common.models import User
from stashbox.backend.common.models.admin_operation_log import AdminOperationLog
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common import quota_service
from stashbox.backend.common.analytics import track_simple
from stashbox.backend.common.events import EventName
from stashbox.backend.common import quota_metrics

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """CP3.6.2：FastAPI lifespan 替代 deprecated @app.on_event("startup")。

    startup：拉起月度配额重置定时器（try/except 包住，不拖垮服务）。
    shutdown：CP3.6.3 先 cancel 后台 task + 关闭其独立 engine，再走普通 teardown。

    CP3.6.3：background task 用独立 engine/pool（bg_engine），不再复用全局
    database.engine。原因：TestClient 关闭时，后台 task 仍占用全局 engine 连接池
    里的连接，而 autouse fixture 在另一 loop 上 dispose 全局 engine，会触发
    asyncpg `RuntimeError: Event loop is closed`。独立 engine 在 lifespan 同一
    loop 上优雅关闭，避免跨 loop 关闭连接。
    """
    # startup：建独立 engine 给 background task
    bg_engine = create_async_engine(
        settings.database_url,
        pool_size=1,
        pool_recycle=settings.postgres_pool_recycle,
        echo=settings.debug,
        future=True,
    )
    bg_session_local = async_sessionmaker(
        bg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    # CP3.6.3：测试环境跳过 bg_task 启动（避免 TestClient teardown 在另一 loop
    # 上关连接，触发 RuntimeError: Event loop is closed）。生产环境通过
    # session_factory 走独立 bg_engine，不占用全局连接池。
    bg_task: asyncio.Task | None = None
    if os.environ.get("PYTEST_CURRENT_TEST") is None:
        try:
            bg_task = asyncio.create_task(
                quota_service.quota_reset_loop(session_factory=bg_session_local)
            )
            log.info("quota_reset_loop started (production)")
        except Exception as exc:
            # 启动失败不能让 user-service 进入 broken state
            log.error("quota_reset_loop 启动失败（忽略）: %s", exc)
    # CP6.2.2.2b 埋点：SERVICE_START（走 bg engine，避免占用全局 engine 连接池）
    try:
        async with bg_session_local() as session:
            await track_simple(session, EventName.SERVICE_START, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass  # 失败不阻塞 startup

    yield

    # shutdown：先 cancel 后台 task + 关闭其独立 engine（同一 loop，避免跨 loop 关连接）
    if bg_task is not None:
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass
    # CP6.2.2.2b 埋点：SERVICE_STOP（同样走 bg engine）
    try:
        async with bg_session_local() as session:
            await track_simple(session, EventName.SERVICE_STOP, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass
    try:
        await bg_engine.dispose()
    except Exception:
        pass

    # CP3.6.3：在 lifespan 同一 loop（仍 open）上清理全局连接池。
    # TestClient 用独立 event loop 跑 app，而 autouse fixture `_dispose_pools`
    # 在另一个 loop 上 dispose 全局 engine / redis pool，会触发
    # `RuntimeError: Event loop is closed`（或 attached to a different loop）。
    # 这里先在本 loop 上优雅关闭全局池，_dispose_pools 再 dispose 时即为空操作。
    try:
        from stashbox.backend.common import database

        try:
            await database.engine.dispose()
        except Exception:
            pass
    except Exception:
        pass
    try:
        from stashbox.backend.common import redis_client

        pool = redis_client._redis_pool
        if pool is not None:
            try:
                await pool.disconnect(inuse_connections=True)
            except Exception:
                pass
            finally:
                # 无论 disconnect 是否成功都置空，避免 _dispose_pools 在错 loop 上再关一次
                redis_client._redis_pool = None
    except Exception:
        pass


setup_logging("user-service")
app = FastAPI(title="stashbox-user-service", version="0.2.0", lifespan=lifespan)
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)


class WechatLoginRequest(BaseModel):
    code: str


class WechatLoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
    expires_in: int


class UserInfo(BaseModel):
    user_id: str
    nickname: str
    avatar: str | None = None


class QuotaInfo(BaseModel):
    plan: str
    total: int
    used: int
    remaining: int


class Plan(BaseModel):
    id: str
    name: str
    price_cny: int
    monthly_quota: int


# ===== CP3.6-A2：管理后台 admin users 2 端点（v1 §3.6 第 1 段：用户管理） =====
class AdminUserItem(BaseModel):
    """GET /admin/users 单条用户。

    v1 §3.6 字段：email / display_name / role / status / last_active_at。

    口径修正（CP-USERS-REALITY）：原注释写「users 表无 email/状态列，故 email=None、
    status 恒 active」，**该前提已不成立** —— 后续 migration 给 users 补了
    `email` 与 `deleted_at` 两列。管理后台却还在按老前提硬编码，于是：
      - 邮箱列全空白（库里明明有值）
      - 状态筛选是个假下拉（选了 suspended/deleted，返回的仍是全部用户）

    现在一律取真实列：
      - email         -> u.email（可能为 None，前端渲染 "—"）
      - display_name  -> nickname
      - role          -> 管理员角色：admin/operator 原样，其余归 user
      - tier          -> users.tier 原值（系统里这一列同时承载"等级"和"管理员角色"，
                         见下方 docstring 的说明，不臆造第二列）
      - status        -> 由 deleted_at 派生：deleted_at 非空 = deleted，否则 active
      - last_active_at-> None（users 表确无该列，不用 listening 之类数据假装）
    """

    id: int
    email: str | None = None
    display_name: str | None = None
    role: str
    tier: str
    status: str
    monthly_quota: int
    used_quota: int
    last_active_at: str | None = None
    created_at: str | None = None


class AdminUserListResponse(BaseModel):
    total: int
    items: list[AdminUserItem]


class QuotaAdjustRequest(BaseModel):
    monthly_quota: int
    reason: str


class UserQuotaResponse(BaseModel):
    id: int
    monthly_quota: int
    used_quota: int
    remaining: int


# ===== CP3.6.2-XIN：admin login 端点 =====
class AdminLoginRequest(BaseModel):
    """POST /api/v1/admin/auth/login。

    email / password 均允许缺省（None），缺失交给路由显式判 400（而非 FastAPI 422），
    以匹配 v1 错误规范「400 缺字段」。
    """

    email: str | None = None
    password: str | None = None


class AdminLoginUser(BaseModel):
    id: int
    email: str | None = None
    display_name: str | None = None
    role: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AdminLoginUser


# ===== CP5.4a-ADMIN：admin 推送队列查询（v1 §3.6 admin 运营端点补齐） =====
class AdminPushNotificationItem(BaseModel):
    """GET /admin/push-notifications 单条。

    字段口径（v1 §端点需求_admin推送队列_v1.md §2.1）：
      - status / error / sent_at 以 push_notifications 表实际列为准,
        表没有则回 None(避免前端误读假数据)。
      - created_at / sent_at / read_at 一律 ISO 8601 字符串(None 时给 None)。
    """

    id: int
    user_id: int
    article_id: str | None = None
    tag_slug: str | None = None
    title: str
    body: str
    deeplink: str | None = None
    status: str
    error: str | None = None
    created_at: str | None = None
    sent_at: str | None = None
    read_at: str | None = None


class AdminPushNotificationListResponse(BaseModel):
    """v1 §接口文档 v1.1 既有约定：{total, limit, offset, items}。"""

    total: int
    limit: int
    offset: int
    items: list[AdminPushNotificationItem]


# ===== CP5.4a-ADMIN-RETRY：admin 失败推送重推（v1 §端点需求_admin推送队列_v1 §2.3） =====
class AdminPushNotificationRetryRequest(BaseModel):
    """POST /admin/push-notifications/{id}/retry body。

    reason 必填且 5..200 字符——与 quota-adjust 同口径,防止审计日志被刷脏。
    """

    reason: str | None = None


class AdminPushNotificationRetryResponse(BaseModel):
    """POST /admin/push-notifications/{id}/retry 响应。

    返回重推后的最新一行（status/error/sent_at 均刷新），让前端无需再 GET 一遍。
    """

    id: int
    user_id: int
    status: str
    error: str | None = None
    sent_at: str | None = None
    retried_at: str  # 服务侧处理时间，与 sent_at 同点（便于 admin 视角分辨「这次 retry 落点」）


@app.get("/health")
async def health():
    return {"status": "ok", "service": "user-service"}


@app.post("/api/v1/auth/wechat-login", response_model=WechatLoginResponse)
async def wechat_login(req: WechatLoginRequest, db: AsyncSession = Depends(get_db)):
    """微信登录（mock：code 派生 open_id，按 open_id 查/插 users 表）。"""
    # 本期不调真实微信 code2session，用 code 派生一个稳定 open_id
    open_id = "wx_" + (req.code or "unknown")[:56]
    result = await db.execute(select(User).where(User.open_id == open_id))
    user = result.scalar_one_or_none()
    if user is None:
        # quota_reset_at 一定要在建号时就写上，别留给 NULL：
        # 定时器的到期判定含 `quota_reset_at IS NULL`，NULL 行会让「任意匿名注册」
        # 命中到期名单（2026-10 修，见 quota_service.quota_reset_loop）。
        user = User(
            open_id=open_id,
            nickname="听友",
            tier="free",
            quota_reset_at=quota_service.next_reset_at_naive(),
        )
        db.add(user)
        await db.flush()  # 先拿 user.id 供埋点
        await db.refresh(user)

    token = create_access_token(str(user.id))
    refresh = create_refresh_token(str(user.id))
    # CP6.2.1 埋点：user_login
    # track() 只 flush 不 commit —— 必须在 commit() 之前，否则埋点随 close() 回滚丢失
    try:
        await track_simple(db, EventName.USER_LOGIN, user.id, "n/a")
    except Exception as exc:
        log.warning(f"USER_LOGIN 埋点异常（忽略）: open_id={open_id} err={exc}")
    await db.commit()

    return WechatLoginResponse(
        access_token=token,
        refresh_token=refresh,
        user_id=str(user.id),
        expires_in=settings.jwt_expire_minutes * 60,
    )


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
    expires_in: int
    tier: str = "free"


@app.post("/api/v1/auth/refresh-token", response_model=RefreshTokenResponse)
async def refresh_token(req: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """用 refresh_token 换新 access_token + 新 refresh_token（rotate）。

    网关已注册此路由（config.py:46 → user-service）。android AuthApi.refresh 调此端点；
    access_token 过期时 AuthInterceptor 自动 refresh 一次并重放请求。
    """
    try:
        payload = decode_refresh_token(req.refresh_token)
    except HTTPException:
        raise Unauthorized(message="invalid or expired refresh token")
    sub = payload.get("sub")
    if not sub:
        raise Unauthorized(message="invalid refresh token payload")
    result = await db.execute(select(User).where(User.id == int(sub)))
    user = result.scalar_one_or_none()
    if user is None:
        raise Unauthorized(message="user not found")
    new_access = create_access_token(str(user.id))
    new_refresh = create_refresh_token(str(user.id))
    return RefreshTokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        user_id=str(user.id),
        expires_in=settings.jwt_expire_minutes * 60,
        tier=user.tier or "free",
    )


@app.get("/api/v1/user", response_model=UserInfo)
async def get_user(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    uid = int(user["sub"])
    result = await db.execute(select(User).where(User.id == uid))
    u = result.scalar_one_or_none()
    if u is None:
        raise NotFound(message=f"user {uid} not found")
    return UserInfo(
        user_id=str(u.id),
        nickname=u.nickname or f"听友_{u.id}",
        avatar=u.avatar_url,
    )


async def _quota_payload(uid: int, db: AsyncSession) -> dict:
    """CP1.6：走 Redis 缓存（miss 查 DB + 回填）。"""
    q = await quota_service.get_quota(db, uid)
    return {
        "user_id": str(uid),
        "monthly_quota": q["monthly_quota"],
        "quota_used": q["quota_used"],
        "remaining": q["monthly_quota"] - q["quota_used"],
        "version": q["version"],
        "reset_at": q.get("reset_at"),
        "cached": bool(q.get("cached")),
    }


@app.get("/api/v1/user/quota")
async def get_quota(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await _quota_payload(int(user["sub"]), db)


@app.get("/api/v1/users/me/quota")
async def get_my_quota(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """CP1.6：`GET /users/me/quota`（v1 §3.3 语义同 /user/quota）。"""
    q = await _quota_payload(int(user["sub"]), db)
    quota_metrics.quota_request_total.labels(endpoint="me_quota").inc()
    return q


@app.post("/api/v1/users/me/quota/reset-monthly")
async def reset_quota_monthly(
    user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    """**全平台**月度重置：把**所有**用户的 quota_used 清零、quota_version+1。

    ⚠️ 命名与语义不符是历史遗留：路径里的 ``me`` 有误导性，实际是全库操作。
    路径保留是为了不打断已存在的调用方，但鉴权必须是 admin —— 这是本函数的
    原实现只有 ``Depends(require_user)`` 时的真实后果：

        quota_service.reset_monthly() 的 UPDATE 没有 User.id 过滤，
        即任意登录用户 POST 一次即可把全平台配额清零 → 直接击穿 LLM/TTS 成本。

    定时器（quota_service.quota_reset_loop，每小时）才是常规路径，本端点只给
    运营在跨月后需要立刻生效时手动触发。
    """
    n = await quota_service.reset_monthly(db)
    # CP6.2.1 埋点：quota_reset（记录实际操作人）
    await track_simple(db, EventName.QUOTA_RESET, int(user["sub"]), "n/a")
    return {"reset_users": n, "scope": "all_users"}


@app.post("/api/v1/users/me/onboarding/start")
async def onboarding_start(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """v1 §11.5 CP5.1 用户进入引导（首次启动）。

    行为：
      1. 校验 user 存在（不存在 404）
      2. 校验未引导过（onboarding_done_at 已设置 → 409 already_done）
      3. 写埋点 ONBOARDING_STARTED
    不修改 user 字段（开始 ≠ 完成）。
    """
    u = await db.get(User, int(user["sub"]))
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.onboarding_done_at is not None:
        raise HTTPException(status_code=409, detail="onboarding already done")

    await track_simple(
        db, EventName.ONBOARDING_STARTED, u.id, await _get_placeholder_article_id(db)
    )
    return {"onboarding_started": True}


# CP5.1: onboarding 埋点需要 article_id（FK 约束），用"系统引导"占位 article
_PLACEHOLDER_ARTICLE_ID: str | None = None


async def _get_placeholder_article_id(db: AsyncSession) -> str:
    """返回任意一个已存在的 article.id，供 onboarding 埋点用（FK 约束）。"""
    global _PLACEHOLDER_ARTICLE_ID
    if _PLACEHOLDER_ARTICLE_ID is None:
        from sqlalchemy import select, text

        row = await db.execute(select(text("id")).select_from(text("articles")).limit(1))
        _PLACEHOLDER_ARTICLE_ID = row.scalar_one_or_none() or "n/a"
    return _PLACEHOLDER_ARTICLE_ID


@app.post("/api/v1/users/me/onboarding/step")
async def onboarding_step_viewed(
    step: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """v1 §11.5 CP5.1 用户看了引导第 N 步。

    行为：
      1. 校验 step ∈ [1, 3]
      2. 校验 user 存在
      3. 写埋点 ONBOARDING_STEP_VIEWED + step 元数据
    """
    if step not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="step must be 1/2/3")
    u = await db.get(User, int(user["sub"]))
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    step_names = {1: "copy_link", 2: "open_d9", 3: "listen_audio"}
    await track_simple(
        db,
        EventName.ONBOARDING_STEP_VIEWED,
        u.id,
        await _get_placeholder_article_id(db),
    )
    return {"step_viewed": step, "step_name": step_names[step]}


@app.post("/api/v1/users/me/onboarding/done")
async def onboarding_complete(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """v1 §11.5 CP5.1 用户完成引导。

    行为：
      1. 校验 user 存在
      2. 设置 onboarding_done_at = now (UTC)
      3. 写埋点 ONBOARDING_COMPLETED
    幂等：重复调用更新 onboarding_done_at = now（v1 §11.5 验收只关心"用户曾完成过"）。
    """
    u = await db.get(User, int(user["sub"]))
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    u.onboarding_done_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # track() 只 flush 不 commit —— 必须在 commit() 之前，否则埋点随 close() 回滚丢失
    try:
        await track_simple(
            db, EventName.ONBOARDING_COMPLETED, u.id, await _get_placeholder_article_id(db)
        )
    except Exception as exc:
        log.warning(f"ONBOARDING_COMPLETED 埋点异常（忽略）: user={u.id} err={exc}")

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {"onboarding_done": True, "onboarding_done_at": u.onboarding_done_at.isoformat()}


@app.get("/api/v1/subscription/plans")
async def get_plans(user: dict = Depends(require_user)):
    plans = [
        Plan(id="free", name="免费", price_cny=0, monthly_quota=5),
        Plan(id="student", name="学生", price_cny=9, monthly_quota=30),
        Plan(id="member", name="会员", price_cny=29, monthly_quota=50),
        Plan(id="pro", name="专业", price_cny=69, monthly_quota=-1),
    ]
    return {"plans": [p.model_dump() for p in plans]}


@app.get("/api/v1/notifications")
async def list_notifications(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    unread_only: bool = False,
    limit: int = 50,
):
    """用户推送列表（CP5.4a）。unread_only=true 仅看未读。"""
    # require_user 返回的是 JWT payload，用户 id 在 "sub"（见本文件其它端点）
    uid = int(user["sub"])
    q = select(PushNotification).where(PushNotification.user_id == uid)
    if unread_only:
        q = q.where(PushNotification.read_at.is_(None))
    q = q.order_by(PushNotification.created_at.desc()).limit(limit)
    result = await db.execute(q)
    notifs = result.scalars().all()
    return {
        "notifications": [
            {
                "id": n.id,
                "article_id": n.article_id,
                "tag_slug": n.tag_slug,
                "title": n.title,
                "body": n.body,
                "deeplink": n.deeplink,
                "read": n.read_at is not None,
                "created_at": n.created_at.isoformat(),
            }
            for n in notifs
        ],
        "unread_count": await db.scalar(
            select(func.count())
            .select_from(PushNotification)
            .where(
                PushNotification.user_id == uid,
                PushNotification.read_at.is_(None),
            )
        ),
    }


@app.post("/api/v1/notifications/{notification_id}/mark-read")
async def mark_notification_read(
    notification_id: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """标记推送已读（CP5.4a）。幂等。"""
    from datetime import datetime

    notif = await db.get(PushNotification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail=f"notification {notification_id} 不存在")
    if notif.user_id != int(user["sub"]):
        # 不能标记别人的推送（防越权）
        raise HTTPException(status_code=403, detail="无权标记他人推送")
    if notif.read_at is None:
        notif.read_at = datetime.now()
        await db.commit()
    return {"ok": True, "read_at": notif.read_at.isoformat()}


# ===== CP5.4a-ADMIN：admin 推送队列端点（v1 §端点需求_admin推送队列_v1 §2.1） =====
_ADMIN_PUSH_STATUS_VALUES = {"pending", "sent", "failed"}


@app.get(
    "/api/v1/admin/push-notifications",
    response_model=AdminPushNotificationListResponse,
)
async def admin_list_push_notifications(
    status: str = "",
    user_id: int | None = None,
    tag_slug: str = "",
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 admin 推送队列全量查询（运营排障）。

    鉴权：require_admin_or_operator（admin / operator 通过；free/viewer 403）。
    只读：不写 admin_operation_logs（与 /admin/consents 同口径）。

    查询参数（v1 §2.1）：
      status     pending|sent|failed；空=全部（非法值直接 400，避免误传命中 0 行）
      user_id    int；空=全部
      tag_slug   str；空=全部
      limit      int；默认 50，cap 200（防单次拉爆）
      offset     int；默认 0
    排序：created_at DESC。
    """
    # 参数校验（commit 前失败不回滚——只读端点无副作用）
    if status and status not in _ADMIN_PUSH_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"status 必须是 pending/sent/failed 之一,收到: {status!r}",
        )
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)

    filters = []
    if status:
        filters.append(PushNotification.status == status)
    if user_id is not None:
        filters.append(PushNotification.user_id == user_id)
    if tag_slug:
        filters.append(PushNotification.tag_slug == tag_slug)

    # 计数 + 查询共用过滤条件
    count_stmt = select(func.count()).select_from(PushNotification)
    list_stmt = select(PushNotification)
    for f in filters:
        count_stmt = count_stmt.where(f)
        list_stmt = list_stmt.where(f)

    total = await db.scalar(count_stmt) or 0
    list_stmt = list_stmt.order_by(PushNotification.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(list_stmt)
    rows = result.scalars().all()

    items = [
        AdminPushNotificationItem(
            id=n.id,
            user_id=n.user_id,
            article_id=n.article_id,
            tag_slug=n.tag_slug,
            title=n.title,
            body=n.body,
            deeplink=n.deeplink,
            status=n.status,
            error=n.error,
            created_at=n.created_at.isoformat() if n.created_at else None,
            sent_at=n.sent_at.isoformat() if n.sent_at else None,
            read_at=n.read_at.isoformat() if n.read_at else None,
        )
        for n in rows
    ]
    return AdminPushNotificationListResponse(total=total, limit=limit, offset=offset, items=items)


# ===== CP5.4a-ADMIN-RETRY：admin 失败推送重推（v1 §端点需求_admin推送队列_v1 §2.3） =====
# reason 长度下限/上限与 quota-adjust 对齐（CP3.6-A2 既有约定）：
#   - < 5：审计空间不足以让运营复盘 → 400，不落库
#   - > 200：刷脏 admin_operation_logs.reason 列 → 400
_ADMIN_PUSH_RETRY_REASON_MIN = 5
_ADMIN_PUSH_RETRY_REASON_MAX = 200


@app.post(
    "/api/v1/admin/push-notifications/{notification_id}/retry",
    response_model=AdminPushNotificationRetryResponse,
)
async def admin_retry_push_notification(
    notification_id: int,
    req: AdminPushNotificationRetryRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §端点需求_admin推送队列_v1 §2.3：失败推送重推。

    鉴权：require_admin_or_operator（admin / operator 通过；free/viewer 403）。
    仅 `status=failed` 可重推（pending 还在队列、sent 已成功，重推无意义）。
    reason 必填且 5..200 字符（与 quota-adjust 同口径）。

    副作用（与目标更新同事务）：
      1. 重置 push_notifications.status='sent' / error=NULL / sent_at=now
         （重置为「已重发」便于 admin 视角立即看效果；真推送 CP4.6 上线后
          可改为 status='pending' 让 worker 重投,改动只需这一行）
      2. 写 admin_operation_logs(action='retry_push_notification',target_id=str(id))
         reason 与请求体一致——便于运营复盘「为什么重推」
    失败整体回滚：reason 校验失败 / status 非 failed / id 不存在 都不会落库。
    """
    # 1) reason 校验（commit 前,失败不落库）
    if not req.reason or not req.reason.strip():
        raise HTTPException(status_code=400, detail="reason 必填")
    reason = req.reason.strip()
    if len(reason) < _ADMIN_PUSH_RETRY_REASON_MIN:
        raise HTTPException(
            status_code=400,
            detail=f"reason 至少 {_ADMIN_PUSH_RETRY_REASON_MIN} 个字符",
        )
    if len(reason) > _ADMIN_PUSH_RETRY_REASON_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"reason 至多 {_ADMIN_PUSH_RETRY_REASON_MAX} 个字符",
        )

    # 2) 目标存在性 + 状态校验
    notif = await db.get(PushNotification, notification_id)
    if notif is None:
        raise HTTPException(status_code=404, detail=f"notification {notification_id} 不存在")
    if notif.status != "failed":
        # 仅 failed 可重推：pending 还在待发队列（不需重投）、sent 已成功（重投无意义）
        raise HTTPException(
            status_code=409,
            detail=(f"仅 status=failed 可重推,当前 status={notif.status!r}"),
        )

    # 3) 副作用：重置状态 + 写 audit（同事务）
    from datetime import datetime as _dt

    now = _dt.now()
    notif.status = "sent"
    notif.error = None
    notif.sent_at = now

    log_row = AdminOperationLog(
        admin_id=int(user["sub"]),
        admin_tier=user.get("tier", "unknown"),
        action="retry_push_notification",
        target_type="push_notification",
        target_id=str(notification_id),
        reason=reason,
        method="POST",
        path=f"/api/v1/admin/push-notifications/{notification_id}/retry",
        request_body={"reason": reason},
        response_status=200,
        ip=None,
        user_agent=None,
    )
    db.add(log_row)

    try:
        await db.commit()
        await db.refresh(notif)
    except Exception:
        await db.rollback()
        raise

    return AdminPushNotificationRetryResponse(
        id=notif.id,
        user_id=notif.user_id,
        status=notif.status,
        error=notif.error,
        sent_at=notif.sent_at.isoformat() if notif.sent_at else None,
        retried_at=now.isoformat(),
    )


@app.get("/api/v1/admin/users", response_model=AdminUserListResponse)
async def admin_list_users(
    page: int = 1,
    size: int = 20,
    keyword: str = "",
    tier: str = "",
    status: str = "",
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 用户管理：分页 + 关键词 + 角色过滤。

    - 关键词：nickname OR email ILIKE '%keyword%'（CP-USERS-REALITY：email 列已存在，
      原实现只搜 nickname，导致有邮箱的用户搜邮箱搜不到）
    - 排序：created_at DESC
    - status：CP-USERS-REALITY 由 deleted_at 派生，**本次真正落库过滤**。
      原实现收了 status 参数却不加任何 where，选 suspended/deleted 返回的还是全量，
      管理后台那个下拉是纯装饰。
    - tier：原样过滤 users.tier

    注意 users 表只有一列 tier，同时承载「等级」和「管理员角色」两种语义
    （admin 用户的 tier 值就是 'admin'）。这是历史建模遗留，红线是不在本次补
    migration，故响应里 role 与 tier 都从这一列派生，靠 role 字段做区分。
    """
    page = max(page, 1)
    size = min(max(size, 1), 100)
    kw = f"%{keyword}%" if keyword else ""

    # 计数 + 查询共用过滤条件
    filters = []
    if kw:
        filters.append(or_(User.nickname.ilike(kw), User.email.ilike(kw)))
    if tier:
        filters.append(User.tier == tier)
    if status == "deleted":
        filters.append(User.deleted_at.isnot(None))
    elif status == "active":
        filters.append(User.deleted_at.is_(None))
    # status == "suspended"：users 表无冻结列，恒不命中任何行。
    # 不静默忽略——否则又变成一个骗人的下拉选项。

    count_stmt = select(func.count()).select_from(User)
    list_stmt = select(User)
    for f in filters:
        count_stmt = count_stmt.where(f)
        list_stmt = list_stmt.where(f)

    total = await db.scalar(count_stmt) or 0
    list_stmt = list_stmt.order_by(User.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(list_stmt)
    users = result.scalars().all()

    items = [
        AdminUserItem(
            id=u.id,
            email=u.email,
            display_name=u.nickname,
            # role 表达「是不是管理员」，tier 表达「等级」——拆开后两列不再重复。
            role=u.tier if u.tier in {"admin", "operator"} else "user",
            tier=u.tier,
            status="deleted" if u.deleted_at else "active",
            monthly_quota=u.monthly_quota,
            used_quota=u.quota_used,
            last_active_at=None,
            created_at=u.created_at.isoformat() if u.created_at else None,
        )
        for u in users
    ]
    return AdminUserListResponse(total=total, items=items)


@app.post("/api/v1/admin/users/{user_id}/quota-adjust", response_model=UserQuotaResponse)
async def admin_quota_adjust(
    user_id: int,
    req: QuotaAdjustRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 调整用户月度配额。

    行为：
      1. 校验 monthly_quota >= 0、reason 必填 ≥5 字符、目标 user 存在
      2. 更新 users.monthly_quota
      3. 同事务写入 admin_operation_logs 一条（A1 已建表）
      4. 失败整体回滚（ValidationError 在 commit 前抛出，不落库）

    CP-USERS-REALITY：原校验是 `monthly_quota <= 0` 拒绝，但 **0 是系统里的真实值**
    —— users.id=1 的 monthly_quota 就是 0（额度耗尽/停用状态），管理员把它设回 0
    是合法操作。同时前端 Users.tsx 校验的是 `value < 0`，两边口径不一致：
    管理员在前端填 0，前端放行、后端 400。这里统一为 >= 0。
    """
    # 校验（commit 前，失败不落库 → 满足“失败回滚事务”）
    if req.monthly_quota < 0:
        raise HTTPException(
            status_code=400, detail="monthly_quota 不能为负数（0 = 额度耗尽，停用该用户）"
        )
    if len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="reason 至少 5 个字符")

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} 不存在")

    # CP-QUOTA-CACHE：配额读走 Redis 缓存（`user:quota:{id}`，TTL 60s），
    # 而本端点改完 monthly_quota **没有失效缓存**。后果是跨端不一致：
    # 管理员在后台把配额调高，用户 App 端最长 60 秒看不到新额度；
    # 把用户停用（=0），用户最长 60 秒内仍能继续剪藏、不会看到任何提示。
    # 跨端 E2E 实测：后台调成 0 之后立刻读 App 的配额接口，拿到的是
    # {"monthly_quota":50,...,"cached":true} —— 还是调整前的值。
    #
    # 与 quota_service.consume 同一套版本号栅栏：quota_version+1 后
    # invalidate_quota(new_version)，让在途的旧值回填被 Lua 栅栏挡住。
    target.monthly_quota = req.monthly_quota
    new_version = (target.quota_version or 0) + 1
    target.quota_version = new_version

    # 审计日志：与目标更新同事务提交，失败一起回滚
    log_row = AdminOperationLog(
        admin_id=int(user["sub"]),
        admin_tier=user.get("tier", "unknown"),
        action="quota_adjust",
        target_type="user",
        target_id=str(user_id),
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/users/{user_id}/quota-adjust",
        request_body={"monthly_quota": req.monthly_quota, "reason": req.reason},
        response_status=200,
        ip=None,
        user_agent=None,
    )
    db.add(log_row)

    try:
        await db.commit()
        await db.refresh(target)
    except Exception:
        await db.rollback()
        raise

    # 提交成功后再失效缓存：commit 之前失效的话，回滚会让缓存变成"已删但库没改"，
    # 下一次读又把旧值回填回去。缓存失效失败不该让 200 变成 5xx ——
    # 数据已经落库了，最多让用户等 TTL 到期。
    try:
        await cache_service.invalidate_quota(user_id, new_version)
    except Exception as exc:  # pragma: no cover - 缓存是加速层，不是正确性来源
        log.warning(
            "QUOTA_CACHE_INVALIDATE_FAILED user=%s ver=%s err=%s", user_id, new_version, exc
        )

    return UserQuotaResponse(
        id=target.id,
        monthly_quota=target.monthly_quota,
        used_quota=target.quota_used,
        remaining=target.monthly_quota - target.quota_used,
    )


# CP3.6.2-XIN admin 登录：邮箱 + 密码（bcrypt）登录，签发 1h JWT。
# 仅 role IN ('admin', 'operator') 可登；成功写一条 admin_operation_logs(ADMIN_LOGIN)。
ADMIN_LOGIN_EXPIRE_SECONDS = 60 * 60  # 1h
_ALLOWED_ADMIN_ROLES = {"admin", "operator"}


@app.post("/api/v1/admin/auth/login", response_model=AdminLoginResponse)
async def admin_login(req: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    """admin / operator 邮箱密码登录，返回 1h JWT + 写审计日志。"""
    # 1) 字段校验（commit 前，缺字段直接 400，不落库）
    if not req.email or not req.password:
        raise BizException(message="email 和 password 必填", code=40000)

    # 2) 按 email 查用户（不存在 → 401，不泄露是否存在）
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="invalid email or password")

    # 3) 密码校验（bcrypt），错误 → 401
    if not user.password_hash or not _verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")

    # 4) 角色校验：仅 admin / operator 可登，否则 403
    if user.tier not in _ALLOWED_ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"admin role required, current tier: {user.tier}",
        )

    # 5) 签发 1h JWT（payload: sub + role + exp + iat；附带 tier 兼容下游依赖）
    token = create_access_token(
        str(user.id),
        extra={"role": user.tier, "tier": user.tier},
        expire_minutes=ADMIN_LOGIN_EXPIRE_SECONDS // 60,
    )

    # 6) 写 admin_operation_logs 一条（与目标查询同会话；失败整体回滚）
    log_row = AdminOperationLog(
        admin_id=user.id,
        admin_tier=user.tier,
        action="ADMIN_LOGIN",
        target_type="user",
        target_id=str(user.id),
        reason="admin login",
        method="POST",
        path="/api/v1/admin/auth/login",
        request_body={"email": req.email},
        response_status=200,
        ip=None,
        user_agent=None,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return AdminLoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=ADMIN_LOGIN_EXPIRE_SECONDS,
        user=AdminLoginUser(
            id=user.id,
            email=user.email,
            display_name=user.nickname,
            role=user.tier,
        ),
    )


def _verify_password(password: str, password_hash: str) -> bool:
    """bcrypt 校验，hash 非法时按失败处理。"""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
