"""
user-service（端口 8001） - 登录 + 配额 + 订阅（mock 实现，in-memory）。

API：
  - GET  /health
  - POST /api/v1/auth/wechat-login   微信登录（公开，mock 签发 JWT）
  - GET  /api/v1/user                当前用户（require_user）
  - GET  /api/v1/user/quota          配额（require_user，mock）
  - GET  /api/v1/subscription/plans  4 层定价（require_user，mock）
"""
import sys
from pathlib import Path

# 让 `import stashbox.backend.common` 可用：仓库根目录的父目录需加入 sys.path
_REPO_PARENT = str(Path(__file__).resolve().parents[3])
if _REPO_PARENT not in sys.path:
    sys.path.insert(0, _REPO_PARENT)

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from stashbox.backend.common.auth import create_access_token, require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import register_exception_handlers
from stashbox.backend.common.logging import setup_logging

setup_logging()
app = FastAPI(title="stashbox-user-service", version="0.1.0")
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
async def wechat_login(req: WechatLoginRequest):
    """微信登录（mock：code 前 16 位作为 user_id）。"""
    user_id = req.code[:16] if req.code else "u_unknown"
    token = create_access_token(user_id)
    return WechatLoginResponse(
        access_token=token,
        user_id=user_id,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@app.get("/api/v1/user", response_model=UserInfo)
async def get_user(user: dict = Depends(require_user)):
    user_id = user.get("sub", "")
    return UserInfo(user_id=user_id, nickname=f"听友_{user_id[-4:]}")


@app.get("/api/v1/user/quota", response_model=QuotaInfo)
async def get_quota(user: dict = Depends(require_user)):
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

    uvicorn.run(app, host="0.0.0.0", port=8001)
