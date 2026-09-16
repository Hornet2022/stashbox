"""
user-service（端口 8101） - 登录 + 配额 + 订阅。

CP1.5：wechat-login / user 走真实 PostgreSQL（users 表）；
quota / subscription/plans 仍为 mock（配额扣减事务在 CP1.6）。
鉴权：JWT 的 sub = users.id（整数），下游据此校验归属。
"""
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
from stashbox.backend.common.models import User

setup_logging()
app = FastAPI(title="stashbox-user-service", version="0.2.0")
register_exception_handlers(app)


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


@app.get("/api/v1/user/quota", response_model=QuotaInfo)
async def get_quota(user: dict = Depends(require_user)):
    # mock（CP1.6 接真实配额扣减事务）
    total = 5
    used = 0
    return QuotaInfo(plan="free", total=total, used=used, remaining=total - used)


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
