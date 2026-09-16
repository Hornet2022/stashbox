"""Arq 配置（CP3.5-pre-3）。

worker 启动：`python -m arq worker.WorkerSettings`（cwd 或 PYTHONPATH 需含 ai-service 目录）。

注意：ai-service 目录名带连字符（不是合法包名），本模块与 dispatcher / worker /
tasks 一样都是顶层模块，导入方式同 llm / distill（见 tests/ai/conftest.py）。
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ArqConfig:
    redis_url: str
    queue_name: str = "stashbox:distill"
    max_jobs: int = 4  # 并发 worker 数
    job_timeout_sec: int = 1800  # 30 分钟
    result_ttl_sec: int = 86400  # 1 天
    retry_max: int = 2  # 失败重试次数（不含首次执行）


def load_arq_config() -> ArqConfig:
    return ArqConfig(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        max_jobs=int(os.getenv("ARQ_MAX_JOBS", "4")),
        job_timeout_sec=int(os.getenv("ARQ_JOB_TIMEOUT", "1800")),
        result_ttl_sec=int(os.getenv("ARQ_RESULT_TTL", "86400")),
        retry_max=int(os.getenv("ARQ_RETRY_MAX", "2")),
    )
