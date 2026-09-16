"""
结构化日志 - structlog + JSON 输出（CP6.4-pre）。

设计要点：
  - 全局 JSON renderer（dev / prod 都是 JSON，便于本机直接 `jq .` 验证）
  - `service` 字段由 setup_logging(service_name) 绑定进 contextvars
  - `request_id` 由 RequestIDMiddleware 每请求绑定（merge_contextvars 自动带上）
  - setup_logging / get_logger API 保持兼容（老调用方不用改）

用法：
    setup_logging("content-service")
    log = get_logger(__name__)
    log.info("xxx_done", article_id=aid)   # kwargs 进 JSON 字段
"""
import logging
import sys
import uuid
from contextvars import ContextVar

import structlog

from stashbox.backend.common.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def _stdout_logger_factory(*_args, **_kwargs):
    """每次都解析当前 sys.stdout —— 否则模块 import 时就把 stdout 定死了，
    pytest capsys / run_dev.sh 重定向都拿不到输出。"""
    return structlog.PrintLogger(file=sys.stdout)


def setup_logging(service_name: str = "unknown", level: str | None = None) -> None:
    """初始化 structlog（每个服务启动时调用一次）。

    level 默认取 settings.log_level，便于按环境调。
    """
    log_level = getattr(logging, (level or settings.log_level).upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=_stdout_logger_factory,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        cache_logger_on_first_use=False,  # 测试里会反复 configure，不能缓存
    )

    # service 进 context：后续所有日志自动带 "service": "content-service"
    structlog.contextvars.bind_contextvars(service=service_name)

    # 降噪（uvicorn / sqlalchemy 走 stdlib logging，这里只压级别）
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")


def get_logger(name: str | None = None):
    """返回 structlog 绑定 logger（注意：调用方式是 log.info(event, **fields)）。"""
    return structlog.get_logger(name)


def new_request_id() -> str:
    """生成并绑定 request_id 到 contextvars（供 structlog 自动输出）。"""
    rid = f"req_{uuid.uuid4().hex[:12]}"
    request_id_ctx.set(rid)
    structlog.contextvars.bind_contextvars(request_id=rid)
    return rid


def bind_request_id(rid: str) -> None:
    """复用上游传入的 request_id（api-gateway → 下游服务链路串联）。"""
    request_id_ctx.set(rid)
    structlog.contextvars.bind_contextvars(request_id=rid)


def get_request_id() -> str:
    return request_id_ctx.get()


def clear_context() -> None:
    """清空 contextvars（单测用，避免 case 之间串 request_id）。"""
    request_id_ctx.set("")
    structlog.contextvars.clear_contextvars()
