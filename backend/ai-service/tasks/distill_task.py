"""Arq worker task：蒸馏任务入口（被 Arq worker 进程调）。

与 BackgroundTasks.add_task 的差异：
- 这里拿不到 FastAPI 的 Request / Depends —— 一切依赖从 ctx 显式传
- 数据库 session 自己开（AsyncSessionLocal）
- 失败抛异常 → Arq 自动 retry（按 retry_max）

CP3.5-pre-3 说明：
- 4 步流水线本身是 CP3.5-pre-2 的 DistillPipeline，本文件只做「参数 → DistillContext」的适配
- 抓取器还没接（CP3.5），raw_content 用占位文本，Step 1 拿到的就是这段占位内容
"""
import structlog

from distill import DistillContext, DistillPipeline
from llm import get_llm_client
from stashbox.backend.common.database import AsyncSessionLocal

log = structlog.get_logger("ai-worker")

# CP3.5 未接抓取器：raw_content 先用占位文本，接抓取后换成正文
RAW_CONTENT_PLACEHOLDER = "[mock raw content] CP3.5 抓取器未接入，Step1 读到的正文是占位文本。"


class _FailingLLM:
    """simulate_failure=True 时的 LLM：任何 chat 都抛错。

    让「模拟失败」走和真实失败完全一样的路径（pipeline → FAILED + 退还配额 + 抛异常），
    避免 main.py 里再维护一套失败分支。
    """

    async def chat(self, req):
        raise RuntimeError("simulated distill failure")

    async def close(self):
        return None


def _build_raw_content(url: str, title: str | None) -> str:
    return f"{RAW_CONTENT_PLACEHOLDER} title={title or '(无标题)'} url={url}"


async def distill_task(
    ctx: dict,
    task_id: str,
    article_id: str,
    user_id: int,
    url: str,
    title: str | None = None,
    simulate_failure: bool = False,
) -> dict:
    """Arq worker task：跑 DistillPipeline。

    Args:
        ctx: Arq 提供的 worker context（含 redis / job_id）
        task_id: distilled_articles.id
        article_id: articles.id
        user_id: 用户 ID
        url: 文章 URL（CP3.5 接抓取器后用于抓正文）
        title: 文章标题
        simulate_failure: 模拟失败（走真实失败路径：FAILED + 退还配额 + 抛异常给 Arq retry）
    """
    log.info("arq_distill_started", task_id=task_id, article_id=article_id)

    pipeline_ctx = DistillContext(
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        url=url,
        title=title,
        raw_content=_build_raw_content(url, title),
    )

    llm = _FailingLLM() if simulate_failure else get_llm_client()
    pipeline = DistillPipeline(llm=llm, db_session_factory=AsyncSessionLocal)

    try:
        await pipeline.run(pipeline_ctx)
        log.info("arq_distill_completed", task_id=task_id, article_id=article_id)
        return {"task_id": task_id, "status": "done"}
    except Exception as e:
        log.exception("arq_distill_failed", task_id=task_id, article_id=article_id, error=str(e))
        raise  # 让 Arq 走 retry 逻辑
    finally:
        await llm.close()
