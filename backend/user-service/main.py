"""
user-service（端口 8101） - 登录 + 配额 + 订阅。

CP1.5：wechat-login / user 走真实 PostgreSQL（users 表）；
quota / subscription/plans 仍为 mock（配额扣减事务在 CP1.6）。
鉴权：JWT 的 sub = users.id（整数），下游据此校验归属。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth import create_access_token, require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import (
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.models import User
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

    yield

    # shutdown（无清理需求，留 CP7 换 apscheduler 再处理）


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
