"""
user-service（端口 8101） - 登录 + 配额 + 订阅。

CP1.5：wechat-login / user 走真实 PostgreSQL（users 表）；
quota / subscription/plans 仍为 mock（配额扣减事务在 CP1.6）。
鉴权：JWT 的 sub = users.id（整数），下游据此校验归属。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth import create_access_token, require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import (
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.auth_admin import require_admin_or_operator
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
    shutdown：no-op（CP3.6.2 阶段不需要清理）。
    """
    # startup
    try:
        import asyncio
        asyncio.create_task(quota_service.quota_reset_loop())
        log.info("quota_reset_loop started")
    except Exception as exc:
        # 启动失败不能让 user-service 进入 broken state
        log.error("quota_reset_loop 启动失败（忽略）: %s", exc)
    # CP6.2.2.2b 埋点：SERVICE_START
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_START, 0, "n/a")
    except Exception:
        pass  # 失败不阻塞 startup

    yield

    # shutdown（无清理需求，留 CP7 换 apscheduler 再处理）
    # CP6.2.2.2b 埋点：SERVICE_STOP
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_STOP, 0, "n/a")
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
    这些列在 users 表尚未建模（无 email/状态/活跃时间列），按现有模型映射：
      - email         -> None（users 表无 email 列，保留字段兼容前端）
      - display_name  -> nickname
      - role          -> tier（v1 §3.6 角色语义即 tier）
      - status        -> "active"（无 soft-delete 状态列，默认 active）
      - last_active_at-> None（users 表无该列）
    不在此追加 migration（CP3.6-A2 红线），仅暴露管理端点。
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
        user = User(open_id=open_id, nickname="听友", tier="free")
        db.add(user)
        await db.commit()
        await db.refresh(user)

    token = create_access_token(str(user.id))
    # CP6.2.1 埋点：user_login
    await track_simple(db, EventName.USER_LOGIN, user.id, "n/a")
    return WechatLoginResponse(
        access_token=token,
        user_id=str(user.id),
        expires_in=settings.jwt_expire_minutes * 60,
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
async def reset_quota_monthly(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """手动触发月度重置（定时器见 quota_service.quota_reset_loop）。"""
    n = await quota_service.reset_monthly(db)
    # CP6.2.1 埋点：quota_reset
    await track_simple(db, EventName.QUOTA_RESET, int(user["sub"]), "n/a")
    return {"reset_users": n}


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
    q = select(PushNotification).where(PushNotification.user_id == user["id"])
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
            select(func.count()).select_from(PushNotification).where(
                PushNotification.user_id == user["id"],
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
    if notif.user_id != user["id"]:
        # 不能标记别人的推送（防越权）
        raise HTTPException(status_code=403, detail="无权标记他人推送")
    if notif.read_at is None:
        notif.read_at = datetime.now()
        await db.commit()
    return {"ok": True, "read_at": notif.read_at.isoformat()}


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

    - 关键词：nickname ILIKE '%keyword%'（v1 §3.6 原指 email/display_name，
      users 表无 email 列，映射为 nickname）
    - 排序：created_at DESC
    - status 查询参数保留接口兼容；users 表无状态列，不做落库过滤
    """
    page = max(page, 1)
    size = min(max(size, 1), 100)
    kw = f"%{keyword}%" if keyword else ""

    # 计数 + 查询共用过滤条件
    filters = []
    if kw:
        filters.append(User.nickname.ilike(kw))
    if tier:
        filters.append(User.tier == tier)

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
            email=None,
            display_name=u.nickname,
            role=u.tier,
            tier=u.tier,
            status="active",
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
      1. 校验 monthly_quota > 0、reason 必填 ≥5 字符、目标 user 存在
      2. 更新 users.monthly_quota
      3. 同事务写入 admin_operation_logs 一条（A1 已建表）
      4. 失败整体回滚（ValidationError 在 commit 前抛出，不落库）
    """
    # 校验（commit 前，失败不落库 → 满足“失败回滚事务”）
    if req.monthly_quota <= 0:
        raise HTTPException(status_code=400, detail="monthly_quota 必须为正整数")
    if len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="reason 至少 5 个字符")

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} 不存在")

    target.monthly_quota = req.monthly_quota

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

    return UserQuotaResponse(
        id=target.id,
        monthly_quota=target.monthly_quota,
        used_quota=target.quota_used,
        remaining=target.monthly_quota - target.quota_used,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
