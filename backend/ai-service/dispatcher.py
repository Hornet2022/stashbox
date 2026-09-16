"""DistillDispatcher：把蒸馏任务塞进 Arq 队列。

接口与 BackgroundTasks.add_task 保持一致（同步调用 → 立即返回），但底层走 Arq +
Redis，任务由独立 worker 进程消费，uvicorn 不再被长任务阻塞。

抽这一层的目的：将来换 Celery / Dramatiq 只改本文件，业务代码（main.py）不动。
"""
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from arq_settings import load_arq_config

DISTILL_TASK_NAME = "distill_task"


class DistillDispatcher:
    def __init__(self, redis_url: str | None = None):
        cfg = load_arq_config()
        self.redis_url = redis_url or cfg.redis_url
        self.queue_name = cfg.queue_name
        self._pool: ArqRedis | None = None

    async def connect(self):
        """建 Redis 连接池（幂等：已连上就直接返回）。"""
        if self._pool is None:
            self._pool = await create_pool(
                RedisSettings.from_dsn(self.redis_url),
                default_queue_name=self.queue_name,
            )

    async def close(self):
        """关连接池（幂等：没连过就 no-op）。"""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def enqueue_distill(
        self,
        task_id: str,
        article_id: str,
        user_id: int,
        url: str,
        title: str | None = None,
        simulate_failure: bool = False,
    ) -> str:
        """入队蒸馏任务，返回 Arq job_id（入队失败时抛异常，由调用方决定降级策略）。

        Args:
            simulate_failure: 模拟失败（用于 E2E 验证 CP1.6 退还路径）
        """
        await self.connect()
        job = await self._pool.enqueue_job(
            DISTILL_TASK_NAME,
            task_id=task_id,
            article_id=article_id,
            user_id=user_id,
            url=url,
            title=title,
            simulate_failure=simulate_failure,
        )
        return job.job_id if job else ""


# 全局单例（lazy init）
_dispatcher: DistillDispatcher | None = None


def get_dispatcher() -> DistillDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = DistillDispatcher()
    return _dispatcher


async def shutdown_dispatcher():
    """关掉全局 dispatcher（FastAPI shutdown 事件 / 单测 teardown 用）。"""
    global _dispatcher
    if _dispatcher is not None:
        await _dispatcher.close()
        _dispatcher = None
