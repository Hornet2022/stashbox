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
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func, or_, update, delete
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
from stashbox.backend.common.models.article import Article
from stashbox.backend.common.models.admin_operation_log import AdminOperationLog
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.models.consent import ConsentRecord
from stashbox.backend.common.models.device_token import DeviceToken
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common import quota_service
from stashbox.backend.common.analytics import track_simple
from stashbox.backend.common.events import EventName
from stashbox.backend.common import quota_metrics
from stashbox.backend.common import rate_limit
from stashbox.backend.common import revocation
from stashbox.backend.common.policies import load_privacy_policy, privacy_policy_version

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


# ── 登录限流（2026-10-21）──────────────────────────────────────────────────
#
# wechat-login 是未鉴权端点，且当前是"任意 code 都能建号"。
# 内测把网关暴露到公网后，一次简单的循环就能建出海量 users，
# 每个号还自带一份免费配额 —— 蒸馏要真花 LLM + TTS 的钱。
#
# 阈值取值：内测是"几十个人手工点登录"的使用强度，20 次/分钟/IP
# 远高于真人操作，同时能挡住脚本。生产接上真微信 OAuth 后可以收紧。
_LOGIN_RATE_LIMIT = int(os.getenv("STASHBOX_LOGIN_RATE_LIMIT", "20"))
_LOGIN_RATE_WINDOW = int(os.getenv("STASHBOX_LOGIN_RATE_WINDOW", "60"))


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（限流按它计数）。

    这里**直接信任** X-Forwarded-For 是安全的，因为：
    - 生产环境只有网关对外，用户连不到 user-service；
    - 网关转发前会**用自己解析出的真实 IP 覆盖** XFF
      （见 api-gateway/main.py 的 _client_ip 与 common/ip.py），
      所以这个头里的值不是客户端能随便伪造的。

    为什么不直接用 common/ip.py 的那套"只信可信代理"的逻辑：
    那个函数是为**对外的入口**写的（要防伪造）；而这里的对端是网关
    自己 —— 它已经把 XFF 净化过了，再套一层"不可信就忽略"会把
    所有用户算成同一个 IP，限流就废了。
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class LogoutResponse(BaseModel):
    """POST /api/v1/auth/logout 响应。"""

    logged_out: bool = True


class UserInfoResponse(BaseModel):
    """GET /api/v1/auth/me 响应。"""

    user_id: str
    nickname: str
    tier: str


@app.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(user: dict = Depends(require_user)):
    """登出：吊销该用户当前签发的所有 access token。

    ## 为什么必须有这个端点
    客户端的「退出登录」一直在调它（[AuthApi.logout]），但**服务端从来没实现过**
    → 一直 404 → 客户端靠 `catch (_: Exception)` 兜住、照样清本地 token，
    **服务端 token 却仍然有效** → 设备丢失、或用户以为登出了却没登出时，
    会话在服务端还能继续用。

    吊销用 [common.revocation]（与「注销账号」同一套机制）：记下吊销时刻，
    校验时按 `iat < revoked_at` 判失效 —— 一次 Redis GET，不查库。
    """
    uid = int(user["sub"])
    await revocation.revoke(uid, at=datetime.now(timezone.utc).timestamp())
    return LogoutResponse(logged_out=True)


@app.get("/api/v1/auth/me", response_model=UserInfoResponse)
async def me(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """当前用户信息。

    客户端定义了 [AuthApi.me]（UserInfoResponse），但服务端一直没实现
    → 端点 404。它当时**无人调用**（死接口），但客户端模型已定义齐全，
    补齐服务端比删客户端更符合原意。
    """
    uid = int(user["sub"])
    row = await db.get(User, uid)
    if row is None:
        raise NotFound(message="用户不存在")
    return UserInfoResponse(
        user_id=str(row.id),
        nickname=row.nickname or "听友",
        tier=row.tier,
    )


@app.post("/api/v1/auth/wechat-login", response_model=WechatLoginResponse)
async def wechat_login(
    req: WechatLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """微信登录（mock：code 派生 open_id，按 open_id 查/插 users 表）。"""
    # 本期不调真实微信 code2session，用 code 派生一个稳定 open_id
    #
    # 正因为"任意 code 都能建号"，这个未鉴权端点必须限流（见上方注释）。
    allowed, _remaining = await rate_limit.check(
        f"ratelimit:login:{_client_ip(request)}",
        limit=_LOGIN_RATE_LIMIT,
        window_sec=_LOGIN_RATE_WINDOW,
    )
    if not allowed:
        raise BizException(
            code=42901,
            message="登录尝试过于频繁，请稍后再试",
            http_status=429,
        )
    open_id = "wx_" + (req.code or "unknown")[:56]
    result = await db.execute(select(User).where(User.open_id == open_id))
    user = result.scalar_one_or_none()
    if user is not None and user.deleted_at is not None:
        # 软删的账号（注销过的）必须**明确拒绝**，不能当成"查不到 → 建新号"。
        #
        # 两个后果，都不是小事：
        #   1. 注销端点承诺「注销后立即停止提供服务」—— 原来软删的 user
        #      照样能登录拿到新 token，注销形同虚设。
        #   2. 注销会把 open_id 匿名化成 `del_<uid>_<random>`，于是旧 code
        #      再登录就查不到了 → 走"建新号"分支 → **users 表多一行**。
        #      内测期清一批测试账号，清理完一登录又长回来 —— 后台用户
        #      列表被这个来回折腾撑大。
        raise BizException(
            code=40102,
            message="该账号已注销，如需继续使用请重新注册",
            http_status=401,
        )
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
    # 登出过的会话不能靠 refresh 复活。
    #
    # 2026-10-22 实测（此前 logout 端点刚补上时）：logout 写了吊销标记，
    # access token 确实 401 了，但拿**同一个 refresh token** 去换新 token
    # 依然返回 200 —— 因为这个端点只查 users.deleted_at，从不看吊销标记。
    # 结果是「登出」只杀了 access token 这条腿，refresh 这条腿还活着：
    # 设备丢了、用户以为登出了，最长 30 天内旧 refresh token 还能无限换出新
    # access token —— 等于给用户发了一张「已登出」的假凭证。
    #
    # 这里补上吊销检查，和 require_user 用同一套判据（common/revocation.py）。
    if await revocation.is_revoked(payload):
        raise Unauthorized(message="Token has been revoked")
    result = await db.execute(select(User).where(User.id == int(sub)))
    user = result.scalar_one_or_none()
    if user is None:
        raise Unauthorized(message="user not found")
    # 2026-10-21：已注销的账号不能换出新 token。
    #
    # 注销时做了两件事：数据库软删（users.deleted_at）+ 吊销已签发的 token
    # （common/revocation.py）。前者是**事实**，后者是**热路径上的快速判定**。
    # 这个端点本来就查库，所以顺手把事实也看一眼 —— Redis 标记万一丢了
    # （Redis 重启），这里仍然是最后一道闸。
    if user.deleted_at is not None:
        raise Unauthorized(message="account has been deleted")
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


# ══════════════════════════════════════════════════════════════════════
# 账号合规组（2026-10-21）
#
# 补齐内测前必须有的三件事：隐私政策同意、注销账号、设备注册（推送投递的前提）。
#
# 这一组端点全是**用户自己**对自己数据的操作，不是管理端 ——
# 所以走 require_user，不走 require_admin。
# ══════════════════════════════════════════════════════════════════════


# ── 隐私政策正文（公开端点，不需要登录）──────────────────────────────
@app.get("/api/v1/policies/privacy")
async def get_privacy_policy():
    """当前隐私政策正文。

    **公开端点**：用户还没登录时也要能读政策（首次启动的同意页就在登录之前）。
    """
    try:
        policy = load_privacy_policy()
    except Exception as exc:  # noqa: BLE001
        # 政策读不出来必须让客户端知道 —— 客户端要靠它显示"你同意的是哪一版"
        log.error("privacy_policy_load_failed: %s", exc)
        raise HTTPException(status_code=503, detail="privacy policy unavailable")
    return policy


# ── 同意记录 ─────────────────────────────────────────────────────────
class ConsentStateResponse(BaseModel):
    #: **用户已同意的**版本号；从来没同意过 = ""。
    #: 客户端拿它跟 current_privacy_policy_version 比：不等 → 要弹同意页。
    privacy_policy_version: str
    #: 服务端当前政策版本。
    current_privacy_policy_version: str
    privacy_policy_accepted: bool
    privacy_policy_accepted_at: str | None
    personalization_enabled: bool
    cross_user_share_enabled: bool


class ConsentUpdateRequest(BaseModel):
    privacy_policy_version: str
    accepted: bool
    personalization_enabled: bool | None = None
    cross_user_share_enabled: bool | None = None


async def _get_or_create_consent(db: AsyncSession, uid: int) -> ConsentRecord:
    """取同意记录；没有就建一行。

    默认值都是"没同意"（privacy_policy_accepted=False）——
    合规上必须是**显式同意**，不能因为"没记录"就当成"同意了"。
    """
    rec = await db.get(ConsentRecord, uid)
    if rec is None:
        rec = ConsentRecord(user_id=uid)
        db.add(rec)
        await db.flush()
    return rec


@app.get("/api/v1/users/me/consent", response_model=ConsentStateResponse)
async def get_my_consent(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """我当前的同意状态。

    客户端用它决定"要不要弹同意页"：当前政策版本 != 已同意的版本 → 弹。
    """
    uid = int(user["sub"])
    rec = await db.get(ConsentRecord, uid)
    current_version = privacy_policy_version()
    if rec is None:
        return ConsentStateResponse(
            privacy_policy_version="",
            current_privacy_policy_version=current_version,
            privacy_policy_accepted=False,
            privacy_policy_accepted_at=None,
            personalization_enabled=False,
            cross_user_share_enabled=False,
        )
    return ConsentStateResponse(
        privacy_policy_version=rec.privacy_policy_version or "",
        current_privacy_policy_version=current_version,
        privacy_policy_accepted=rec.privacy_policy_accepted,
        privacy_policy_accepted_at=(
            rec.privacy_policy_accepted_at.isoformat() if rec.privacy_policy_accepted_at else None
        ),
        personalization_enabled=rec.personalization_enabled,
        cross_user_share_enabled=rec.cross_user_share_enabled,
    )


@app.put("/api/v1/users/me/consent", response_model=ConsentStateResponse)
async def update_my_consent(
    req: ConsentUpdateRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """记录 / 撤回同意。

    允许**撤回**：accepted=false 是合法操作（合规要求"撤回同意不影响撤回前已进行的处理"）。

    版本号必须与服务端当前版本一致 —— 客户端不能拿着旧版本号来"同意"，
    那等于同意了一份用户没看过的政策。
    """
    uid = int(user["sub"])
    current_version = privacy_policy_version()
    if req.privacy_policy_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"policy version mismatch: client sent {req.privacy_policy_version!r}, "
                f"current is {current_version!r}"
            ),
        )

    rec = await _get_or_create_consent(db, uid)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rec.privacy_policy_accepted = req.accepted
    rec.privacy_policy_version = current_version if req.accepted else None
    rec.privacy_policy_accepted_at = now if req.accepted else None
    if req.personalization_enabled is not None:
        rec.personalization_enabled = req.personalization_enabled
    if req.cross_user_share_enabled is not None:
        rec.cross_user_share_enabled = req.cross_user_share_enabled

    try:
        await track_simple(db, EventName.USER_CONSENT_UPDATED, uid, "n/a")
    except Exception as exc:
        log.warning(f"USER_CONSENT_UPDATED 埋点异常（忽略）: user={uid} err={exc}")

    try:
        await db.commit()
        await db.refresh(rec)
    except Exception:
        await db.rollback()
        raise

    return ConsentStateResponse(
        privacy_policy_version=rec.privacy_policy_version or "",
        current_privacy_policy_version=current_version,
        privacy_policy_accepted=rec.privacy_policy_accepted,
        privacy_policy_accepted_at=(
            rec.privacy_policy_accepted_at.isoformat() if rec.privacy_policy_accepted_at else None
        ),
        personalization_enabled=rec.personalization_enabled,
        cross_user_share_enabled=rec.cross_user_share_enabled,
    )


# ── 注销账号 ─────────────────────────────────────────────────────────
class AccountDeleteRequest(BaseModel):
    """注销请求。

    confirm_text 必须精确等于 :data:`ACCOUNT_DELETE_CONFIRM_TEXT`。
    为什么要"打字确认"：注销是**不可逆**的终局操作，一个误触就永久丢数据。
    这不是 UX 洁癖 —— 是让用户有机会意识到自己在做什么。
    """

    confirm_text: str
    reason: str | None = None


class AccountDeleteResponse(BaseModel):
    deleted: bool
    already_deleted: bool
    anonymized_fields: list[str]
    articles_soft_deleted: int


#: 注销确认短语。客户端要显示出来让用户照着输入。
ACCOUNT_DELETE_CONFIRM_TEXT = "注销听匣"


@app.post("/api/v1/users/me/account/delete", response_model=AccountDeleteResponse)
async def delete_my_account(
    req: AccountDeleteRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """注销账号（删除我的账号）。

    ## 做什么

    1. **匿名化**（立即，不可逆）：open_id / union_id / phone / apple_id / email /
       avatar_url / password_hash 清空，nickname 改成"已注销用户"，open_id 换成
       一个**不可逆**的随机串（保留原值没有意义，但也得满足 unique 约束）。
    2. **软删**该用户的全部文章（articles.deleted_at）—— 音频文件在 OSS 上，
       由运营按 30 天期限批量清理（不在这个请求里做）。
    3. **停用**该用户的全部设备推送令牌。
    4. **删掉**同意记录（那是关于"这个人"的个人信息）。
    5. **吊销**该用户已签发的所有 token（见 common/revocation.py）——
       这一步是"注销后立即停止提供服务"的兑现点。
    6. 软删用户行本身（users.deleted_at）。

    ## 幂等

    重复调用返回 200 + already_deleted=true，不报错 ——
    客户端重试（网络抖动）不该让用户看到"注销失败"。

    ## 为什么是软删而不是硬删

    - articles 挂着外键到 users，硬删会连带删掉别人的评论/标签关联
    - 审计与计费口径需要保留"曾经有过这个人"的痕迹（但不含个人内容）
    - 真要物理删除，是运营侧的批量清理任务（30 天期限），不在用户请求里做
    """
    if (req.confirm_text or "").strip() != ACCOUNT_DELETE_CONFIRM_TEXT:
        raise HTTPException(
            status_code=400,
            detail=f"confirm_text must be exactly {ACCOUNT_DELETE_CONFIRM_TEXT!r}",
        )

    uid = int(user["sub"])
    u = await db.get(User, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    if u.deleted_at is not None:
        # 已经注销过了 —— 幂等返回，不做任何写入
        return AccountDeleteResponse(
            deleted=True,
            already_deleted=True,
            anonymized_fields=[],
            articles_soft_deleted=0,
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1) 匿名化
    anonymized: list[str] = []
    if u.open_id:
        # 换成随机串：原值是微信 OpenID，属于个人信息，不能留。
        # 保留 id 前缀只为了运维能看出"这是被注销的那个号"，不泄露任何身份。
        u.open_id = f"del_{uid}_{secrets.token_hex(8)}"
        anonymized.append("open_id")
    for field in ("union_id", "phone", "apple_id", "email", "avatar_url", "password_hash"):
        if getattr(u, field, None):
            setattr(u, field, None)
            anonymized.append(field)
    if u.nickname:
        u.nickname = "已注销用户"
        anonymized.append("nickname")

    # 2) 软删文章
    res = await db.execute(
        update(Article)
        .where(Article.user_id == uid, Article.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
    )
    articles_soft_deleted = int(res.rowcount or 0)

    # 3) 停用设备推送令牌
    await db.execute(
        update(DeviceToken)
        .where(DeviceToken.user_id == uid, DeviceToken.deleted_at.is_(None))
        .values(is_active=False, deleted_at=now, updated_at=now)
    )

    # 4) 删同意记录
    await db.execute(delete(ConsentRecord).where(ConsentRecord.user_id == uid))

    # 5) 软删用户行
    u.deleted_at = now
    u.quota_used = 0
    u.monthly_quota = 0

    try:
        await track_simple(db, EventName.USER_ACCOUNT_DELETED, uid, "n/a")
    except Exception as exc:
        log.warning(f"USER_ACCOUNT_DELETED 埋点异常（忽略）: user={uid} err={exc}")

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 6) 吊销 token —— 放在 commit **之后**。
    #    顺序很关键：先落库（数据确实没了），再吊销（服务确实停了）。
    #    反过来的话，中间崩了会出现"数据还在但 token 已废"——用户重登就能看到
    #    自己已注销的账号，比反过来更糟。
    #
    #    ⚠️ 时间戳必须是 **UTC**。上面算的 `now` 是 naive 的（为了写 DB 的
    #    TIMESTAMP WITHOUT TIME ZONE 列），直接 `.timestamp()` 会被 Python
    #    按**本地时区**解释 —— 东八区就比真实 UTC 早 8 小时，
    #    于是"吊销时刻"落在 token 签发时刻**之前**，判据 `iat < revoked_after`
    #    恒为假，token 根本没被吊销。
    #    这条 bug 是被 test_delete_revokes_token_immediately 抓出来的。
    await revocation.revoke(uid, at=now.replace(tzinfo=timezone.utc).timestamp())

    return AccountDeleteResponse(
        deleted=True,
        already_deleted=False,
        anonymized_fields=anonymized,
        articles_soft_deleted=articles_soft_deleted,
    )


# ── 设备注册（推送投递的前提）─────────────────────────────────────────
class DeviceRegisterRequest(BaseModel):
    token: str
    platform: str = "android"
    provider: str = "none"
    app_version: str | None = None
    device_model: str | None = None


class DeviceResponse(BaseModel):
    id: int
    token: str
    platform: str
    provider: str
    app_version: str | None
    device_model: str | None
    is_active: bool


@app.get("/api/v1/users/me/devices", response_model=list[DeviceResponse])
async def list_my_devices(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """我注册过的设备。"""
    uid = int(user["sub"])
    res = await db.execute(
        select(DeviceToken)
        .where(DeviceToken.user_id == uid, DeviceToken.deleted_at.is_(None))
        .order_by(DeviceToken.updated_at.desc())
    )
    rows = res.scalars().all()
    return [
        DeviceResponse(
            id=r.id,
            token=r.token,
            platform=r.platform,
            provider=r.provider,
            app_version=r.app_version,
            device_model=r.device_model,
            is_active=r.is_active,
        )
        for r in rows
    ]


@app.post("/api/v1/users/me/devices", response_model=DeviceResponse)
async def register_my_device(
    req: DeviceRegisterRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """注册（或更新）一台设备的推送令牌。

    **幂等**：同一个 (token, provider) 再次注册 = 更新归属 + 重新激活。
    换账号登录时，客户端会用新账号的 token 调一次这里，把这台设备转过去 ——
    否则会出现"登出后旧账号还在往这台手机推"的情况。
    """
    uid = int(user["sub"])
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    provider = (req.provider or "none").strip()[:32]

    res = await db.execute(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.provider == provider)
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = DeviceToken(
            user_id=uid,
            token=token,
            platform=(req.platform or "android")[:16],
            provider=provider,
            app_version=req.app_version,
            device_model=req.device_model,
        )
        db.add(row)
    else:
        # 归属转移 + 重新激活
        row.user_id = uid
        row.platform = (req.platform or row.platform)[:16]
        row.app_version = req.app_version
        row.device_model = req.device_model
        row.is_active = True
        row.deleted_at = None

    try:
        await db.commit()
        await db.refresh(row)
    except Exception:
        await db.rollback()
        raise

    return DeviceResponse(
        id=row.id,
        token=row.token,
        platform=row.platform,
        provider=row.provider,
        app_version=row.app_version,
        device_model=row.device_model,
        is_active=row.is_active,
    )


@app.delete("/api/v1/users/me/devices/{device_id}", response_model=AccountDeleteResponse)
async def unregister_my_device(
    device_id: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """解绑一台设备（停用，不硬删 —— 留着才知道"这台设备曾经推过"）。"""
    uid = int(user["sub"])
    row = await db.get(DeviceToken, device_id)
    if row is None or row.user_id != uid:
        raise HTTPException(status_code=404, detail="device not found")
    row.is_active = False
    row.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return AccountDeleteResponse(
        deleted=True, already_deleted=False, anonymized_fields=[], articles_soft_deleted=0
    )


# ── 应用版本（公开端点，驱动应用内更新）─────────────────────────────
class AppVersionResponse(BaseModel):
    latest_version_code: int
    latest_version_name: str
    min_supported_version_code: int
    download_url: str
    release_notes: str
    published_at: str | None
    mandatory: bool


@app.get("/api/v1/app/version", response_model=AppVersionResponse)
async def get_app_version():
    """当前发布的 App 版本信息（应用内更新用）。

    **公开端点**：更新检查发生在"用户还没登录"和"App 刚启动"的时候。

    值从环境变量读（见 .env.example）：
      STASHBOX_APP_LATEST_VERSION_CODE / _NAME / _DOWNLOAD_URL
      STASHBOX_APP_MIN_SUPPORTED_VERSION_CODE / _NOTES

    为什么用环境变量而不是 DB：内测阶段每次发版都要改一次，
    而"改一个环境变量 + 重启 user-service"比"加一个管理页面"轻得多。
    真要运营自助，改成 SystemConfig 即可（表已存在）。
    """

    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, "") or default)
        except ValueError:
            return default

    latest_code = _int_env("STASHBOX_APP_LATEST_VERSION_CODE", 0)
    min_code = _int_env("STASHBOX_APP_MIN_SUPPORTED_VERSION_CODE", 0)
    return AppVersionResponse(
        latest_version_code=latest_code,
        latest_version_name=os.getenv("STASHBOX_APP_LATEST_VERSION_NAME", ""),
        min_supported_version_code=min_code,
        download_url=os.getenv("STASHBOX_APP_DOWNLOAD_URL", ""),
        release_notes=os.getenv("STASHBOX_APP_RELEASE_NOTES", ""),
        published_at=os.getenv("STASHBOX_APP_PUBLISHED_AT", "") or None,
        mandatory=latest_code > 0 and min_code > 0 and min_code >= latest_code,
    )


@app.get("/api/v1/subscription/plans")
async def get_plans(user: dict = Depends(require_user)):
    plans = [
        # 免费档配额跟配置走（STASHBOX_FREE_TIER_QUOTA，默认 50）——
        # 付费墙显示的"免费档 N 篇"必须和实际发到的配额一致，
        # 否则用户看到的数字和撞到的墙对不上。
        Plan(
            id="free",
            name="免费",
            price_cny=0,
            monthly_quota=settings.free_tier_monthly_quota,
        ),
        # 档位必须**单调递增**：免费档提到 50 之后，学生档还是 30 就成了
        # "学生花 9 块只有 30 篇，比免费用户还少" —— 定价逻辑自相矛盾。
        Plan(id="student", name="学生", price_cny=9, monthly_quota=150),
        Plan(id="member", name="会员", price_cny=29, monthly_quota=500),
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
    list_stmt = (
        list_stmt.order_by(PushNotification.created_at.desc(), PushNotification.id.desc())
        .offset(offset)
        .limit(limit)
    )
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
    reason 必填且 5..200 字符（与 quota-adjust 同口径）。

    ## 2026-10-21：它现在**真的会推**

    原来这个端点的副作用是：

        notif.status = "sent"; notif.error = None; notif.sent_at = now

    —— 直接把状态改成"已发送"，**一个网络请求都不发**。
    运营在后台看到"已发送"，用户手机上什么都没收到。
    那是整条推送链路上最坏的一种状态：**看起来是绿的，实际上没做**。

    现在走真的投递：取该用户的设备令牌 → 调 provider → 按真实结果写状态。
    没配通道、用户没注册设备、网关报错，都会如实写 failed + 原因。

    允许重推的状态：failed / pending / sending（sent 不给重推，
    因为那会真的给用户再发一遍）。
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
    if notif.status == "sent":
        # 已成功投递的不给重推（重推会真的再发一次，用户收到两条）
        raise HTTPException(
            status_code=409,
            detail="该通知已投递成功，不需重推",
        )

    # 3) 真投递（2026-10-21）
    #
    #    原来这里是 `notif.status = "sent"` —— 直接把状态改成已发送，
    #    **不发起任何投递请求**。运营在后台看到"已发送"，
    #    用户手机上什么都没收到，而系统里一切正常。
    #    （那行代码的注释自己都写着"真推送 CP4.6 上线后可改为 status='pending'"）
    #
    #    现在走真的投递路径：取设备 → 调 provider → 按**真实结果**写状态。
    #    没有配置通道 / 用户没注册设备 / 网关报错，都会如实写 failed + 原因。
    from stashbox.backend.common import push_service

    notif.status = push_service.STATUS_PENDING
    await db.flush()
    result = await push_service.deliver_notification(db, notif)
    log.info(
        "admin_retry_push: notif=%s provider=%s -> %s",
        notification_id,
        result.provider,
        result.summary(),
    )

    log_row = AdminOperationLog(
        admin_id=await _resolve_audit_admin_id(db, user.get("sub")),
        admin_tier=user.get("tier", "unknown"),
        action="retry_push_notification",
        target_type="push_notification",
        target_id=str(notification_id),
        reason=reason,
        method="POST",
        path=f"/api/v1/admin/push-notifications/{notification_id}/retry",
        # 把真实投递结果也记进审计 —— 运营复盘"为什么这条推不出去"时用得上
        request_body={
            "reason": reason,
            "provider": result.provider,
            "attempted": result.attempted,
            "delivered": result.delivered,
            "failed": result.failed,
            "detail": result.detail,
        },
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
        retried_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
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
    # 排序必须带**唯一 tiebreaker**（id）。
    #
    # 为什么：只按 created_at DESC 排时，同一时刻创建的多个用户（批量导入、
    # 测试连建几个账号、并发注册）created_at 完全相同 → PostgreSQL 对并列
    # 元素的排序**不稳定** → 两次查询的并列部分顺序可能不同 →
    # **同一个用户会同时出现在第 1 页和第 2 页，另一个人两页都看不到**。
    #
    # 实测（2026-10-22）：后台用户列表翻页，断言「两页 id 不重叠」时
    # 撞上重复 id（{46762} & {46762}）。运营看到的现象是「翻页有重复、
    # 人数对不上」—— 典型到会被当成「数据脏了」，而根因只是少一个排序键。
    #
    # id 是主键，天然唯一 → 加它之后排序是**全序**，翻页结果稳定可复现。
    list_stmt = (
        list_stmt.order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
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


async def _resolve_audit_admin_id(db: AsyncSession, sub: str | None) -> int | None:
    """把 JWT 的 sub 解析成可写进 admin_operation_logs.admin_id 的值。

    为什么必须有这一步：`admin_operation_logs.admin_id` 上有
    `FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL`。
    原来的写法是 `admin_id=int(user["sub"])` —— 直接相信 token 里的 sub
    一定对应某个 users 行。**它并不保证**：

      · 跨端 E2E 签的 admin token 用 sub='9018'（沿用 tests/e2e/conftest.py
        的约定），而 users 表里**从来没有 9018 这一行**（生产库和隔离库都实测
        count=0）；
      · 任何用非 users 主键签出来的 token 都一样。

    后果不只是审计行写不进去：审计行和业务改动**同事务**（见 quota-adjust 的
    「与目标更新同事务提交，失败一起回滚」），于是 commit 时 flush 审计 INSERT
    撞外键 → IntegrityError → **整个事务回滚** → 管理员的配额改动被静默丢弃，
    接口回 500。实测：POST /api/v1/admin/users/1/quota-adjust 返回 500，
    而 users.monthly_quota 并未被改动。

    `ON DELETE SET NULL` 说明这张表本就允许「admin 不存在」（被删掉的账号仍要留
    审计痕迹），只是**从未存在**的 sub 没被兜住。所以这里查不到就写 NULL：
    审计行照样落库（action/target/reason 都在），只是不挂到某个具体账号上。

    刻意**不**用 try/except 包住 INSERT：那样只是把外键冲突换个地方冒出来，
    并且仍然回滚业务改动。这里是把「可能写不进去」变成「一开始就不写」。
    """
    try:
        admin_id = int(sub)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if admin_id <= 0:
        return None
    exists = await db.scalar(select(User.id).where(User.id == admin_id))
    return admin_id if exists is not None else None


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
        admin_id=await _resolve_audit_admin_id(db, user.get("sub")),
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

    # 3.5) 已注销（软删）的账号不能登后台。
    #
    # wechat 登录（:440）和 refresh（:523）都查了 users.deleted_at，
    # 只有 admin 登录漏了 —— 结果是「注销账号」这个承诺对后台不生效：
    # 注销过的 admin/operator 照样拿密码登进管理后台。
    # 三处登录口径必须一致，判据也只有一条：deleted_at is not None → 拒绝。
    if user.deleted_at is not None:
        raise HTTPException(status_code=401, detail="account has been deleted")

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
