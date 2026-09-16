"""
content-service（端口 8102） - 文章 CRUD + 待听/听过/收藏/跳过 + 标签 + D9 回调。

CP1.5：全部走真实 PostgreSQL（articles 表）。
数据隔离：文章按 user_id 归属，非 owner 访问详情/操作返回 403。
软删除：删除走 updated deleted_at（本服务不直接删除，CP1.6 再加）。
"""
import uuid
from datetime import datetime
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import (
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.models import Article, User

setup_logging()
app = FastAPI(title="stashbox-content-service", version="0.2.0")
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AddArticleRequest(BaseModel):
    url: str
    source: str = "web"  # wechat | douyin | web | pdf | d9 | clawbot


class ArticleResponse(BaseModel):
    id: str
    url: str
    source: str
    title: str | None = None
    owner_id: str
    status: str  # pending | distilling | ready | listened | failed
    favorite: bool
    skip: bool
    created_at: str


class D9AddRequest(BaseModel):
    url: str
    title: str | None = None


class ClawBotMessageRequest(BaseModel):
    text: str
    user_id: str | None = None


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "content-service"}


async def _create_article(
    url: str, user_id: int, source: str, title: str | None, db: AsyncSession
) -> Article:
    art = Article(
        id=_new_article_id(),
        user_id=user_id,
        url=url,
        source=source,
        title=title,
        status="pending",
        favorite=False,
        skip=False,
    )
    db.add(art)
    await db.commit()
    await db.refresh(art)
    return art


@app.post("/api/v1/articles/add", response_model=ArticleResponse)
async def add_article(
    req: AddArticleRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _create_article(req.url, int(user["sub"]), req.source, None, db)
    return _to_response(art)


@app.get("/api/v1/articles/pending")
async def list_pending(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Article).where(
            Article.user_id == int(user["sub"]),
            Article.status.in_(["pending", "distilling", "ready"]),
            Article.skip.is_(False),
            Article.deleted_at.is_(None),
        )
    )
    items = result.scalars().all()
    items = [_to_response(a) for a in items]
    return {"articles": items, "count": len(items)}


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
    art = await _get_owned(article_id, int(user["sub"]), db)
    return _to_response(art)


@app.post("/api/v1/articles/{article_id}/mark-listened")
async def mark_listened(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _get_owned(article_id, int(user["sub"]), db)
    art.status = "listened"
    await db.commit()
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


@app.post("/api/v1/callback/d9-add-article", response_model=ArticleResponse)
async def d9_add_article(
    req: D9AddRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """D9 入口（核心）：微信「更多打开方式」→ 听匣，加入文章。"""
    art = await _create_article(req.url, int(user["sub"]), "d9", req.title, db)
    return _to_response(art)


@app.post("/api/v1/callback/clawbot-message")
async def clawbot_message(
    req: ClawBotMessageRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """ClawBot 入口（mock）：接收消息，解析出 URL 则建文章。"""
    art = None
    if "http" in req.text:
        art = await _create_article(req.text, int(user["sub"]), "clawbot", None, db)
    return {"received": True, "article": _to_response(art) if art else None}


@app.get("/api/v1/tags")
async def list_tags(user: dict = Depends(require_user)):
    tags = [
        {"id": "t_tech", "name": "科技", "category": "subject"},
        {"id": "t_finance", "name": "财经", "category": "subject"},
        {"id": "t_life", "name": "生活", "category": "subject"},
        {"id": "t_news", "name": "时事", "category": "subject"},
    ]
    return {"tags": tags}


@app.get("/api/v1/admin/stats")
async def admin_stats(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
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
    return {
        "total_users": total_users or 0,
        "total_articles": total_articles or 0,
        "pending": pending or 0,
        "listened": listened or 0,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
