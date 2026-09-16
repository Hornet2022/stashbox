"""
ai-service（端口 8003） - L4 蒸馏 worker（mock 流水线）+ 任务状态机。

本期为 mock：用 asyncio.sleep(2) 模拟每步耗时，不调真实 LLM / TTS（CP3 才接）。
数据隔离：任务按 owner_id 归属。
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让 `import stashbox.backend.common` 可用：仓库根目录的父目录需加入 sys.path
_REPO_PARENT = str(Path(__file__).resolve().parents[3])
if _REPO_PARENT not in sys.path:
    sys.path.insert(0, _REPO_PARENT)

from fastapi import BackgroundTasks, Depends, FastAPI
from pydantic import BaseModel

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import (
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging

setup_logging()
app = FastAPI(title="stashbox-ai-service", version="0.1.0")
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# in-memory 任务存储
# ---------------------------------------------------------------------------
_tasks: dict[str, dict] = {}
_counter = 0

_STEPS = [
    ("step1", "多模态理解（Qwen2.5-VL mock）", 0.25),
    ("step2", "听感改写（Claude Sonnet mock）", 0.50),
    ("step3", "TTS 合成（豆包 TTS mock）", 0.75),
    ("step4", "音频拼接（FFmpeg mock）", 1.00),
]


def _next_task_id() -> str:
    global _counter
    _counter += 1
    return f"task_{_counter:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_pipeline(task_id: str) -> None:
    """mock 4 步蒸馏流水线。"""
    task = _tasks.get(task_id)
    if task is None:
        return
    task["status"] = "running"
    for step_key, step_name, progress in _STEPS:
        task["current_step"] = step_key
        task["current_step_name"] = step_name
        task["progress"] = progress
        await asyncio.sleep(2)
    task["status"] = "done"
    task["progress"] = 1.0
    task["current_step"] = None
    task["audio_url"] = (
        f"https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com/{task['article_id']}.m4a"
    )
    task["finished_at"] = _now()


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
    return {"status": "ok", "service": "ai-service"}


@app.post("/api/v1/distill/start", response_model=DistillStartResponse)
async def distill_start(
    req: DistillStartRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_user),
):
    task_id = _next_task_id()
    _tasks[task_id] = {
        "task_id": task_id,
        "article_id": req.article_id,
        "url": req.url,
        "title": req.title,
        "owner_id": user["sub"],
        "status": "queued",
        "progress": 0.0,
        "current_step": None,
        "current_step_name": None,
        "audio_url": None,
        "created_at": _now(),
        "finished_at": None,
    }
    background_tasks.add_task(_run_pipeline, task_id)
    return DistillStartResponse(
        task_id=task_id, article_id=req.article_id, status="queued"
    )


@app.get("/api/v1/distill/{task_id}")
async def distill_status(task_id: str, user: dict = Depends(require_user)):
    task = _tasks.get(task_id)
    if task is None:
        raise NotFound(message=f"task {task_id} not found")
    if task["owner_id"] != user["sub"]:
        from stashbox.backend.common.exceptions import Forbidden

        raise Forbidden(message="not the owner of this task")
    return task


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
