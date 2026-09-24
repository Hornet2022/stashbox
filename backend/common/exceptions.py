"""
业务异常 + 全局处理。

约定:
    业务异常 -> 业务码 + 4xx HTTP
    未捕获 -> 500 + 内部错误码
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from loguru import logger  # noqa  # 简化日志


class BizException(Exception):
    """业务异常基类"""

    code: int = 40000
    message: str = "Business error"
    http_status: int = 400

    def __init__(
        self,
        message: str | None = None,
        code: int | None = None,
        http_status: int | None = None,
    ):
        if message:
            self.message = message
        if code:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.message)


class NotFound(BizException):
    code = 40400
    message = "Resource not found"
    http_status = 404


class InvalidRequest(BizException):
    """请求参数非法（CP TTS-Config：补齐之前 admin_router 隐式依赖的全局名）。"""

    code = 40000
    message = "Invalid request"
    http_status = 400


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

    # F2（P1 接口一致性）：把裸 Starlette HTTPException（如 user-service 大量
    # `raise HTTPException(..., detail=...)`）统一包成 {code, message, data}，
    # 让 android 能读到业务 code 驱动 付费墙(3001)/重登(40100)/音频未就绪(40400)
    # 等 UX 分支。BizException 由上面的 biz_exception_handler 处理，不受影响。
    # 4 个服务（content/ai/user/gateway）都调本函数注册，一处修改全局生效。
    _STATUS_CODE_MAP = {
        400: 40000,
        401: 40100,
        403: 40300,
        404: 40400,
        409: 40900,
        422: 42200,
        429: 42900,
        500: 50000,
    }

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = _STATUS_CODE_MAP.get(exc.status_code, exc.status_code * 100)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": code, "message": str(exc.detail), "data": None},
        )
