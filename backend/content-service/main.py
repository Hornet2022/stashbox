"""
content-service（端口 8002） - 文章 CRUD + 待听/听过/收藏/跳过 + 标签 + D9 回调（mock）。

本期为 in-memory dict 存储，**不连真实 DB**（CP1.5 才接 PostgreSQL）。
数据隔离：文章按 owner_id 归属，非 owner 访问详情返回 403。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让 `import stashbox.backend.common` 可用：仓库根目录的父目录需加入 sys.path
_REPO_PARENT = str(Path(__file__).resolve().parents[3])
if _REPO_PARENT not in sys.path:
    sys.path.insert(0, _REPO_PARENT)

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import (
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging

setup_logging()
app = FastAPI(title="stashbox-content-service", version="0.1.0")
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# in-memory 存储
# ---------------------------------------------------------------------------
_articles: dict[str, dict] = {}
_counter = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return f"art_{_counter:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_owned(article_id: str, user_id: str) -> dict:
    art = _articles.get(article_id)
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art["owner_id"] != user_id:
        raise Forbidden(message="not the owner of this article")
    return art


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AddArticleRequest(BaseModel):
    url: str
    source: str = "web"  # wechat | douyin | web | pdf | d9


class ArticleResponse(BaseModel):
    id: str
    url: str
    source: str
    title: str | None = None
    owner_id: str
    status: str  # pending | listened
    favorite: bool
    skip: bool
    created_at: str


class D9AddRequest(BaseModel):
    url: str
    title: str | None = None


class ClawBotMessageRequest(BaseModel):
    text: str
    user_id: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "content-service"}


def _create_article(url: str, owner_id: str, source: str, title: str | None) -> dict:
    aid = _next_id()
    art = {
        "id": aid,
        "url": url,
        "source": source,
        "title": title,
        "owner_id": owner_id,
        "status": "pending",
        "favorite": False,
        "skip": False,
        "created_at": _now(),
    }
    _articles[aid] = art
    return art


@app.post("/api/v1/articles/add", response_model=ArticleResponse)
async def add_article(req: AddArticleRequest, user: dict = Depends(require_user)):
    art = _create_article(req.url, user["sub"], req.source, None)
    return art


@app.get("/api/v1/articles/pending")
async def list_pending(user: dict = Depends(require_user)):
    items = [
        a for a in _articles.values()
        if a["owner_id"] == user["sub"] and a["status"] == "pending" and not a["skip"]
    ]
    return {"articles": items, "count": len(items)}


@app.get("/api/v1/articles/listened")
async def list_listened(user: dict = Depends(require_user)):
    items = [
        a for a in _articles.values()
        if a["owner_id"] == user["sub"] and a["status"] == "listened"
    ]
    return {"articles": items, "count": len(items)}


@app.get("/api/v1/articles/{article_id}", response_model=ArticleResponse)
async def get_article(article_id: str, user: dict = Depends(require_user)):
    return _get_owned(article_id, user["sub"])


@app.post("/api/v1/articles/{article_id}/mark-listened")
async def mark_listened(article_id: str, user: dict = Depends(require_user)):
    art = _get_owned(article_id, user["sub"])
    art["status"] = "listened"
    return {"id": article_id, "status": "listened"}


@app.post("/api/v1/articles/{article_id}/favorite")
async def favorite(article_id: str, user: dict = Depends(require_user)):
    art = _get_owned(article_id, user["sub"])
    art["favorite"] = True
    return {"id": article_id, "favorite": True}


@app.post("/api/v1/articles/{article_id}/skip")
async def skip(article_id: str, user: dict = Depends(require_user)):
    art = _get_owned(article_id, user["sub"])
    art["skip"] = True
    return {"id": article_id, "skip": True}


@app.post("/api/v1/callback/d9-add-article", response_model=ArticleResponse)
async def d9_add_article(req: D9AddRequest, user: dict = Depends(require_user)):
    """D9 入口（核心）：微信「更多打开方式」→ 听匣，加入文章。"""
    art = _create_article(req.url, user["sub"], "d9", req.title)
    return art


@app.post("/api/v1/callback/clawbot-message")
async def clawbot_message(req: ClawBotMessageRequest, user: dict = Depends(require_user)):
    """ClawBot 入口（mock）：接收消息，解析出 URL 则建文章。"""
    art = None
    if "http" in req.text:
        art = _create_article(req.text, user["sub"], "clawbot", None)
    return {"received": True, "article": art}


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
async def admin_stats(user: dict = Depends(require_user)):
    total = len(_articles)
    pending = sum(1 for a in _articles.values() if a["status"] == "pending")
    listened = sum(1 for a in _articles.values() if a["status"] == "listened")
    return {
        "total_articles": total,
        "pending": pending,
        "listened": listened,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
