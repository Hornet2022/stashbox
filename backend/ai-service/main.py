"""
ai-service（端口 8103） - L4 蒸馏 worker（mock 流水线）+ 任务状态机。

CP1.5：蒸馏任务写真实 PostgreSQL（distilled_articles 表），状态机 queued → running → done。
本期 mock：用 asyncio.sleep(2) 模拟每步耗时，不调真实 LLM / TTS（CP3 才接）。
不读 Article 表（CP3 才接 D9 → 蒸馏全链路）；distill/{id} 经 articles 校验归属。
"""
import asyncio
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import BackgroundTasks, Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import (
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.models import Article, DistilledArticle
from stashbox.backend.common.redis_client import get_redis_pool

setup_logging()
app = FastAPI(title="stashbox-ai-service", version="0.2.0")
register_exception_handlers(app)


# mock 4 步蒸馏流水线（本期不落库每一步，仅用其耗时模拟）
_STEPS = [
    ("step1", "多模态理解（Qwen2.5-VL mock）", 0.25),
    ("step2", "听感改写（Claude Sonnet mock）", 0.50),
    ("step3", "TTS 合成（豆包 TTS mock）", 0.75),
    ("step4", "音频拼接（FFmpeg mock）", 1.00),
]


def _new_task_id() -> str:
    return f"dst_{uuid.uuid4().hex[:24]}"


async def _run_pipeline(task_id: str) -> None:
    """mock 4 步蒸馏流水线，结果写回 distilled_articles。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DistilledArticle).where(DistilledArticle.id == task_id)
        )
        da = result.scalar_one_or_none()
        if da is None:
            return
        da.status = "running"
        await session.commit()

        for _step_key, _step_name, _progress in _STEPS:
            await asyncio.sleep(2)

        da.status = "done"
        da.audio_url = (
            f"https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com/{da.article_id}.m4a"
        )
        da.duration_sec = 300
        da.tags = ["科技", "商业"]
        da.quality_score = 8.5
        await session.commit()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class DistillStartRequest(BaseModel):
    article_id: str
    url: str
    title: str | None = None


class DistillStartResponse(BaseModel):
    task_id: str
    article_id: str
    status: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    redis_status = "ok"
    try:
        client = redis.Redis(connection_pool=get_redis_pool())
        await client.ping()
        await client.aclose()
    except Exception:
        redis_status = "error"
    return {"status": "ok", "service": "ai-service", "redis": redis_status}


@app.post("/api/v1/distill/start", response_model=DistillStartResponse)
async def distill_start(
    req: DistillStartRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    task_id = _new_task_id()
    da = DistilledArticle(
        id=task_id,
        article_id=req.article_id,
        status="queued",
        audio_url=None,
        script_text=None,
    )
    db.add(da)
    await db.commit()
    background_tasks.add_task(_run_pipeline, task_id)
    return DistillStartResponse(task_id=task_id, article_id=req.article_id, status="queued")


@app.get("/api/v1/distill/{task_id}")
async def distill_status(
    task_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(DistilledArticle).where(DistilledArticle.id == task_id)
    )
    da = result.scalar_one_or_none()
    if da is None:
        raise NotFound(message=f"task {task_id} not found")

    # 经 articles 表校验归属
    art_result = await db.execute(select(Article).where(Article.id == da.article_id))
    article = art_result.scalar_one_or_none()
    if article is None or article.user_id != int(user["sub"]):
        raise Forbidden(message="not the owner of this task")

    return {
        "task_id": da.id,
        "article_id": da.article_id,
        "status": da.status,
        "audio_url": da.audio_url,
        "duration_sec": da.duration_sec,
        "tags": da.tags,
        "quality_score": da.quality_score,
        "created_at": da.created_at.isoformat() if da.created_at else None,
        "updated_at": da.updated_at.isoformat() if da.updated_at else None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8103)
