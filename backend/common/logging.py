"""
结构化日志 - JSON（生产）/ 彩色 console（开发）。
"""
import json
import logging
import sys
from datetime import datetime
from typing import Any

from stashbox.backend.common.config import settings


class JSONFormatter(logging.Formatter):
    """生产环境用 JSON 格式，方便 Loki/ES 解析"""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        # 业务自定义字段（extra={"user_id": 123}）
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs", "message",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "exc_info", "exc_text", "taskName",
            ):
                log_data[key] = value
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging() -> None:
    """初始化日志（每个服务启动时调用一次）"""
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if settings.environment == "prod":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # 降噪
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
