"""Arq Worker 入口（CP3.5-pre-3）。

启动（二选一）：
    cd backend/ai-service && ../.venv/bin/python -m arq worker.WorkerSettings
    PYTHONPATH=<repo>/backend/ai-service ../.venv/bin/arq worker.WorkerSettings

注意：ai-service 目录名带连字符（不是合法包名），worker / arq_settings / tasks
都是顶层模块 —— 所以要么 cwd 是 ai-service（`python -m` 会把 cwd 加进 sys.path），
要么把 ai-service 目录放进 PYTHONPATH。
"""
import asyncio

from arq import run_worker
from arq.connections import RedisSettings

from arq_settings import load_arq_config
from tasks.distill_task import distill_task

_cfg = load_arq_config()


class WorkerSettings:
    """Arq WorkerSettings（Arq 通过名字反射调用，别在这上面写逻辑）。"""

    functions = [distill_task]
    redis_settings = RedisSettings.from_dsn(_cfg.redis_url)
    queue_name = _cfg.queue_name
    max_jobs = _cfg.max_jobs
    job_timeout = _cfg.job_timeout_sec
    keep_result = _cfg.result_ttl_sec
    # Arq 的 max_tries 含首次执行；retry_max 指「额外重试次数」
    retry_jobs = _cfg.retry_max > 0
    max_tries = _cfg.retry_max + 1

    health_check_interval = 30


def main():
    """同步入口（开发用）：`python -m worker`（arq CLI 内部走的是同一条路径）。"""
    asyncio.run(run_worker(WorkerSettings))


if __name__ == "__main__":
    main()
