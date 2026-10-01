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
from prometheus_client import start_http_server

from arq_settings import load_arq_config
from tasks.distill_task import distill_task
from variant_transcode_task import variant_transcode_task

_cfg = load_arq_config()


class WorkerSettings:
    """Arq WorkerSettings（Arq 通过名字反射调用，别在这上面写逻辑）。"""

    functions = [distill_task, variant_transcode_task]
    redis_settings = RedisSettings.from_dsn(_cfg.redis_url)
    queue_name = _cfg.queue_name
    max_jobs = _cfg.max_jobs
    job_timeout = _cfg.job_timeout_sec
    keep_result = _cfg.result_ttl_sec
    # Arq 的 max_tries 含首次执行；retry_max 指「额外重试次数」
    retry_jobs = _cfg.retry_max > 0
    max_tries = _cfg.retry_max + 1

    health_check_interval = 30

    async def on_startup(ctx):
        """CP11.0.3: 启动独立的 metrics HTTP server，端口 8104。

        CP-LOGGING-WORKER：必须在这里调 setup_logging。
        arq worker 不经过 FastAPI 启动流程，之前**从未**初始化过日志：

          - structlog 靠自带默认 PrintLogger 还能输出（所以 distill_task 的日志看得见）；
          - 标准库 logging 完全没 handler，于是 agent/runner.py 里
            `logging.getLogger("agent.runner")` 的日志**全部被静默丢弃** ——
            agent 节点层在生产日志里完全不可见，排障时分不清
            「节点没执行」还是「执行了但没打日志」。
        """
        from stashbox.backend.common.logging import setup_logging

        setup_logging("ai-worker")

        from prometheus_client import start_http_server

        try:
            start_http_server(8104)
        except Exception:
            pass


def main():
    """同步入口（开发用）：`python -m worker`（arq CLI 内部走的是同一条路径）。"""
    # CP11.0.3: 启动独立的 metrics HTTP server，让 distill histogram counter 可被 ai-service 抓到
    # worker 进程有自己的 Prometheus registry（与 FastAPI 进程隔离），需独立端口
    try:
        start_http_server(8104)
    except Exception:
        pass
    asyncio.run(run_worker(WorkerSettings))


if __name__ == "__main__":
    main()
