"""
ai-service（端口 8103） - L4 蒸馏 worker（mock 流水线）+ 任务状态机。

CP1.5：蒸馏任务写真实 PostgreSQL（distilled_articles 表），状态机 queued → running → done。
本期 mock：用 asyncio.sleep(2) 模拟每步耗时，不调真实 LLM / TTS（CP3 才接）。
不读 Article 表（CP3 才接 D9 → 蒸馏全链路）；distill/{id} 经 articles 校验归属。
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common import cache_service, quota_service
from stashbox.backend.common.auth import require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import (
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.models import Article, DistilledArticle
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.redis_client import get_redis_pool

from dispatcher import get_dispatcher, shutdown_dispatcher

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """CP3.6.1：FastAPI lifespan 替代 deprecated @app.on_event。

    startup 期间启动队列 poller（try/except 包住，不拖垮服务）。
    shutdown 期间关闭 Arq 连接池。
    """
    # ---- startup: 队列 poller ----
    poll_task = None
    try:
        from observability.metrics import DISTILL_QUEUE_SIZE
        from arq_settings import load_arq_config

        cfg = load_arq_config()
        queue_name = cfg.queue_name
        redis_url = cfg.redis_url

        async def _poll_loop():
            client = aioredis.from_url(redis_url)
            while True:
                try:
                    size = await client.zcard(queue_name)
                    DISTILL_QUEUE_SIZE.labels(queue=queue_name).set(size)
                except Exception as exc:
                    log.warning("queue poller 失败（忽略）: %s", exc)
                await asyncio.sleep(30)

        poll_task = asyncio.create_task(_poll_loop())
        log.info("distill queue poller 已启动")
    except Exception as exc:
        log.error("distill queue poller 启动失败（忽略）: %s", exc)

    yield

    # ---- shutdown: 关闭 poller + Arq ----
    if poll_task:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
    await shutdown_dispatcher()


setup_logging("ai-service")
app = FastAPI(title="stashbox-ai-service", version="0.2.0", lifespan=lifespan)
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)


# mock 4 步蒸馏流水线（本期不落库每一步，仅用其耗时模拟）
_STEPS = [
    ("step1", "多模态理解（Qwen2.5-VL mock）", 0.25),
    ("step2", "听感改写（Claude Sonnet mock）", 0.50),
    ("step3", "TTS 合成（豆包 TTS mock）", 0.75),
    ("step4", "音频拼接（FFmpeg mock）", 1.00),
]


def _new_task_id() -> str:
    return f"dst_{uuid.uuid4().hex[:24]}"


async def _run_pipeline(task_id: str, simulate_failure: bool = False) -> None:
    """mock 4 步蒸馏流水线，结果写回 distilled_articles。

    CP1.6：simulate_failure=True 时走失败分支 → failed + 退还配额。
    真实蒸馏失败（CP3 接 LLM/TTS 后）走同一条退还路径。

    CP3.5-pre-3：端点已改用 Arq（见 dispatcher.enqueue_distill / tasks.distill_task），
    本函数保留作 sync fallback（紧急回滚可临时切回 BackgroundTasks，单测也直接用它）。
    """
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

        if simulate_failure:
            da.status = "failed"
            await session.commit()
            # 蒸馏失败 → 退还配额（quota_used-1, quota_version+1）+ 缓存失效
            result = await session.execute(select(Article).where(Article.id == da.article_id))
            art = result.scalar_one_or_none()
            if art is not None:
                await quota_service.refund(session, int(art.user_id))
                await cache_service.clear_article_quota(da.article_id)  # 退还后允许重扣
            return

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
    job_id: str = ""  # CP3.5-pre-3：Arq job_id（BackgroundTasks 时代没有）


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """遗留别名（CP1.6 在用）：CP6.4 起新增 /healthz /readyz，本端点保持不动。"""
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
    # CP3.5-pre-3：BackgroundTasks.add_task → Arq 队列（独立 worker 进程消费）
    job_id = await get_dispatcher().enqueue_distill(
        task_id=task_id,
        article_id=req.article_id,
        user_id=int(user["sub"]),
        url=req.url,
        title=req.title,
    )
    return DistillStartResponse(
        task_id=task_id, article_id=req.article_id, status="queued", job_id=job_id
    )


@app.post("/api/v1/articles/{article_id}/distill")
async def distill_article(
    article_id: str,
    simulate_failure: bool = False,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """开始蒸馏（v1 §3.2）：已在该文章上做过完整蒸馏则直接复用（按 article 幂等）；

    否则扣一次配额 → 建任务 → 入 Arq 队列。

    - CP1.7.4 幂等修复：复用判定从「已扣过配额」改为「已有完整蒸馏产物」
      （不再把已在 content-service 抓取阶段扣过的配额误算入蒸馏阶段）
    - simulate_failure=True 用于验证「蒸馏失败 → 退还」
    - CP3.5-pre-3：任务不再在请求线程里跑（BackgroundTasks），而是塞进 Arq 队列由
      独立 worker 进程消费；端点签名不变，只多返一个 job_id
    """
    uid = int(user["sub"])
    art_result = await db.execute(select(Article).where(Article.id == article_id))
    art = art_result.scalar_one_or_none()
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art.user_id != uid:
        raise Forbidden(message="not the owner of this article")

    # 幂等：该文章已有蒸馏任务 → 不再扣配额
    existed = await db.scalar(
        select(func.count()).select_from(DistilledArticle).where(
            DistilledArticle.article_id == article_id
        )
    )
    already_charged = existed > 0
    quota_used = None
    if not already_charged:
        quota = await quota_service.consume(db, uid)  # 用尽抛 3001
        quota_used = quota["quota_used"]
        await cache_service.mark_article_quota(article_id)

    task_id = _new_task_id()
    da = DistilledArticle(
        id=task_id,
        article_id=article_id,
        status="queued",
        audio_url=None,
        script_text=None,
    )
    db.add(da)
    art.status = "distilling"
    await db.commit()

    job_id = await get_dispatcher().enqueue_distill(
        task_id=task_id,
        article_id=article_id,
        user_id=uid,
        url=art.url,
        title=art.title,
        simulate_failure=simulate_failure,
    )

    if quota_used is None:
        quota_used = (await quota_service.get_quota(db, uid))["quota_used"]
    return {
        "article_id": article_id,
        "task_id": task_id,
        "job_id": job_id,
        "status": "started",
        "quota_consumed": not already_charged,
        "quota_used": quota_used,
    }


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
