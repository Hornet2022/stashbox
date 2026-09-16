"""
业务异常 + 全局处理。

约定:
    业务异常 -> 业务码 + 4xx HTTP
    未捕获 -> 500 + 内部错误码
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger  # noqa  # 简化日志


class BizException(Exception):
    """业务异常基类"""

    code: int = 40000
    message: str = "Business error"
    http_status: int = 400

    def __init__(self, message: str | None = None, code: int | None = None):
        if message:
            self.message = message
        if code:
            self.code = code
        super().__init__(self.message)


class NotFound(BizException):
    code = 40400
    message = "Resource not found"
    http_status = 404


class Unauthorized(BizException):
    code = 40100
    message = "Unauthorized"
    http_status = 401


class Forbidden(BizException):
    code = 40300
    message = "Forbidden"
    http_status = 403


class QuotaExceeded(BizException):
    code = 42900
    message = "Quota exceeded"
    http_status = 429


def register_exception_handlers(app: FastAPI) -> None:
    """注册到 FastAPI app"""

    @app.exception_handler(BizException)
    async def biz_exception_handler(request: Request, exc: BizException):
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": 42200,
                "message": "Validation error",
                "data": {"errors": exc.errors()},
            },
        )
