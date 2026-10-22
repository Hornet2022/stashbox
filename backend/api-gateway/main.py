"""
api-gateway（端口 8100） - 听匣统一入口 + JWT 签发 + 路由分发。

职责：
  - GET  /health                                 健康检查
  - POST /api/v1/auth/token                     签发 JWT（公开）
  - 路由表命中的请求（见 config.ROUTES）          按表转发到对应下游
  - /api/v1/{path:path}                          表外兜底：按前缀猜下游

路由表（CP1.7.1）：route → service 的映射全部在 config.py，main.py 只做
动态注册 + 统一转发。CP1.7.1 新增 3 条：
  POST /api/v1/callback/d9-add-article      -> content-service（不要求登录态）
  GET  /api/v1/articles/{article_id}/status -> content-service
  GET  /api/v1/articles/{article_id}/audio-url -> content-service
  未匹配                                          -> 404
"""

from contextlib import asynccontextmanager
import os
import sys
from pathlib import Path

# api-gateway 没有 __init__.py，是按脚本模块加载。
# 生产路径（uvicorn 启动）：cwd=backend/，api-gateway/ 自动在 sys.path 上。
# 单测路径（importlib 加载 main.py）：cwd 不固定，把自身目录加入 sys.path。
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from config import D9_ROUTE, ROUTES, Route  # noqa: E402
from stashbox.backend.common.auth import create_access_token, create_refresh_token
from stashbox.backend.common.config import settings
from stashbox.backend.common.exceptions import register_exception_handlers
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.middleware.audit import AuditMiddleware
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.event_collect import router as event_router
from stashbox.backend.common.analytics import track_simple
from stashbox.backend.common.events import EventName
from stashbox.backend.common.database import AsyncSessionLocal


class ErrorTrackingMiddleware(BaseHTTPMiddleware):
    """5xx structlog 警告埋点（CP6.2.2.2b）。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.status_code >= 500:
            structlog.get_logger("api_5xx").warning(
                "api_5xx",
                path=request.url.path,
                status=response.status_code,
                method=request.method,
            )
        return response


setup_logging("api-gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # trust_env=False：网关只代理到本机下游服务(localhost:810x)，绝不走系统 HTTP 代理。
    # 否则在有 HTTP_PROXY 的环境里(http_proxy 指向 127.0.0.1:xxxx)，httpx 会把
    # localhost:8102 等内网调用也转给代理，代理连不上本机 → upstream connect refused →
    # 网关对所有代理路由返回 502/500（蒸馏/订阅/回听等功能集体报错）。下游服务自身调
    # 外部 LLM 用的是各自独立的 client，不受此处影响。
    app.state.httpx = httpx.AsyncClient(trust_env=False)
    # CP6.2.2.2b 埋点：SERVICE_START
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_START, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass  # 失败不阻塞 startup
    yield
    # P1-5：shutdown 时等待 audit task 写完（避免 admin log 漏写）
    from stashbox.backend.common.middleware.audit import drain_pending_audit_tasks

    await drain_pending_audit_tasks(timeout=5.0)
    # CP6.2.2.2b 埋点：SERVICE_STOP
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_STOP, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass
    await app.state.httpx.aclose()


app = FastAPI(
    title="stashbox-api-gateway",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(ErrorTrackingMiddleware)
app.add_middleware(AuditMiddleware)
install_health_endpoints(app)
# CORS: 显式配置优先；没配就看环境。
#
# 2026-10-21 修：原来兜底是 ["*"] + allow_credentials=True。
# 那等价于**任何网站**都能带着 admin cookie 调本网关的 /api/v1/admin/*，
# 运营后台的 cookie 会被恶意站点带着发出去（CSRF）。
# 而且 "*" + credentials 这个组合本身就不是合法 CORS 响应头
# （浏览器要求 Access-Control-Allow-Origin 不能是 "*" 且同时带 credentials），
# 实际是靠 Starlette 在带 cookie 时回显 Origin 才凑合能用 —— 能用不等于对。
#
# 现在的口径（对齐本仓库既有的"生产必须显式配置否则启动失败"惯例，
# 见 common/config.py 的 jwt_secret 断言、android 的 release signing 门禁）：
#   - 配了 STASHBOX_CORS_ALLOW_ORIGINS → 用它（逗号分隔）
#   - prod 且没配 → **启动即失败**，并写清怎么填
#   - dev/staging 且没配 → 只放行本机开发端口，不放行全网
_cors_env = os.getenv("STASHBOX_CORS_ALLOW_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
elif settings.environment == "prod":
    raise RuntimeError(
        "生产环境必须显式配置 STASHBOX_CORS_ALLOW_ORIGINS（逗号分隔的 origin 白名单）。"
        "例如：https://stashbox.cn,https://admin.stashbox.cn。"
        "不配置就启动失败是有意的：默认值放行全网等于把运营后台的 cookie 暴露给任意站点。"
    )
else:
    # 本机开发：admin-web dev 5173 / e2e 预览 4174。
    _cors_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4174",
        "http://127.0.0.1:4174",
    ]
# 必须最后 add：FastAPI 中间件倒序执行（最后 add 最先 run = 最外层）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 音频回放：v1 §3.6 明确规定「OSS 是唯一写音频的职责」，gateway 不再本地挂载。
# 客户端拿到的 audio_url 已是 OSS 公网/签名 URL（content-service 直签 OSS 返回），
# gateway 只做透传，不再做 static mount。
#
# CP9.x：本地开发 / 内网测试场景下 STORAGE_PROVIDER=local，audio_url 形如
#   http://10.0.2.2:8100/audio/{key} （或 http://172.16.x.x:8100/audio/{key} 真机）
# 此时需要 gateway 挂 /audio/ 静态路由兜底。开启条件（任一）：
#   - STORAGE_PROVIDER=local
#   - ENABLE_LOCAL_AUDIO_MOUNT=1（生产也可手动开，但默认关闭）
if (
    os.getenv("STORAGE_PROVIDER", "oss").lower() == "local"
    or os.getenv("ENABLE_LOCAL_AUDIO_MOUNT") == "1"
):
    _audio_dir = os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio")
    if os.path.isdir(_audio_dir):
        app.mount("/audio", StaticFiles(directory=_audio_dir, check_dir=False), name="local-audio")
        structlog.get_logger("api-gateway").info("local_audio_mounted", directory=_audio_dir)


class TokenRequest(BaseModel):
    user_id: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-gateway"}


@app.post("/api/v1/auth/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest):
    """开发调试专用：直接用传入 user_id 签发 JWT。

    ⚠️ 这是一个**无鉴权的后门** —— 任何能访问网关的人传任意 user_id 就能拿到
    该账号的有效 token。生产环境绝对不能开着。

    2026-10-02 审计发现：此端点原本是裸装饰器，无任何门控，实测在生产网关
    （:8100）上直接可用，等同于任意账号接管。已改为默认关闭。

    真正的登录走 user-service 的 `POST /api/v1/auth/wechat-login`
    （由 code 派生 open_id → 查/建 users 行 → 签发），安卓 App 已切到那条路径。

    本地调试要开：显式设 `STASHBOX_ALLOW_DEV_TOKEN=1`，且仅限非 prod 环境。
    两者缺一即 403 —— 单设 env 不足以在 prod 打开。
    """
    if not os.getenv("STASHBOX_ALLOW_DEV_TOKEN"):
        raise HTTPException(
            status_code=403,
            detail=(
                "dev token 端点已关闭。真实登录请用 POST /api/v1/auth/wechat-login；"
                "本地调试可设 STASHBOX_ALLOW_DEV_TOKEN=1。"
            ),
        )
    if settings.environment == "prod":
        raise HTTPException(status_code=403, detail="dev token 端点在 prod 环境禁用")

    token = create_access_token(req.user_id)
    refresh = create_refresh_token(req.user_id)
    return TokenResponse(
        access_token=token,
        refresh_token=refresh,
        expires_in=settings.jwt_expire_minutes * 60,
    )


# ── 网关 → 下游的调用超时（秒）────────────────────────────────────────────
#
# 必须 **大于** 下游最慢的同步端点，否则网关会在下游还在干活时先断开，
# 客户端拿到一个没有业务信封的裸 500（安卓的四个错误码分支全部落空）。
#
# 2026-10-21 实测：剪藏（POST /api/v1/articles）的抓取预算
# STASHBOX_CAPTURE_FETCH_BUDGET 默认 30s，加上无头浏览器兜底的实际开销，
# 整条链最坏 ~40.6s；而网关原 timeout=30.0 **必然先超时**
# （实测网关 30.012s 返回裸 500，同一请求直连下游 40.6s 返回正常的
# 502 + {"code":2002,...} 信封）。
#
# 60s 留足浏览器兜底余量；仍可用环境变量覆盖。
try:
    _UPSTREAM_TIMEOUT = float(os.getenv("STASHBOX_GATEWAY_UPSTREAM_TIMEOUT", "60"))
except ValueError:
    _UPSTREAM_TIMEOUT = 60.0

# 剪藏类路径：抓取在下游是**同步**的（建文章前必须先拿到正文），
# 慢的时候能跑满整个抓取预算。这些路径超时时，文案必须告诉用户
# 「可能已经提交」—— 因为下游其实已经把文章建好、配额也扣了。
_CAPTURE_PATHS = frozenset({"/api/v1/articles", "/api/v1/articles/add"})


def _client_ip(request: Request) -> str:
    """解析真实客户端 IP（见 common/ip.py 的说明）。

    网关是唯一对外的入口，它解析出来的 IP 会**覆盖**转发给下游的
    X-Forwarded-For —— 下游（user-service 的登录限流）据此计数。
    """
    from stashbox.backend.common.ip import client_ip_from_request

    return client_ip_from_request(request, settings.trusted_proxy_ips)


def _set_forwarded_for(headers: dict[str, str], request: Request) -> None:
    """把 headers 里的 X-Forwarded-For **替换**成网关解析出的真实 IP。

    ⚠️ 必须先删掉所有大小写变体再写新的：
    ASGI 把请求头名规范成小写（"x-forwarded-for"），而我们写的是
    "X-Forwarded-For"。不删的话 httpx 会把**两个**头一起发出去，
    下游读到的还是客户端伪造的那个 —— 覆盖就等于没做。
    """
    for key in [k for k in headers if k.lower() == "x-forwarded-for"]:
        del headers[key]
    headers["X-Forwarded-For"] = _client_ip(request)


def _biz_envelope(status_code: int, code: int, message: str) -> JSONResponse:
    """业务信封。

    网关的 4xx/5xx 必须是 {code,message,data}，不能是裸文本 ——
    安卓 parseEnvelope 靠这个信封分流（2001/2002 剪藏失败、3001 配额用尽、
    40400 音频未就绪、40100 会话失效），解析不出来就只剩一句无意义的 500。
    """
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "data": None},
    )


def _upstream_error_response(path: str, exc: Exception) -> JSONResponse:
    """下游不可达 / 超时 → 业务信封。

    2026-10-21 修：原来 httpx 的异常直接冒泡给 Starlette，
    返回的是纯文本 "Internal Server Error"（content-type: text/plain）。
    后果不是"报错不好看"这么简单 —— 安卓的 isCaptureFetchFailed(2001|2002)
    判定依赖信封里的业务码，裸 500 让它恒为假，用户看到的是"服务暂不可用"，
    而后端那边文章已经建好、配额已经扣了。用户会反复重试，越扣越多。
    """
    if isinstance(exc, httpx.TimeoutException):
        if path in _CAPTURE_PATHS:
            # 剪藏超时的特殊性：下游多半**已经**建好文章并扣了配额，
            # 只是回应没赶上。文案必须说清"可能已提交"，
            # 否则用户只会当成失败，然后重试 → URL 去重虽然能挡住重复建文章，
            # 但重试的每一次都会重新走一遍抓取。
            return _biz_envelope(
                502,
                2002,
                "抓取耗时较长，这篇文章可能已经提交了，请稍后在文章列表查看",
            )
        return _biz_envelope(504, 50400, "服务处理超时，请稍后重试")
    # 连不上下游：下游没起来 / 网络断了 / 进程崩了
    return _biz_envelope(502, 50200, "暂时连不上后端服务，请稍后重试")


async def proxy(request: Request, route: Route) -> Response:
    """统一代理：method / path / query / body / headers 全透传给下游。

    CP1.7.1 不注入 service token（service-to-service auth 留 CP1.8+），
    Authorization 原样带给下游，由下游自己校验 JWT。
    """
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    # 链路串联：客户端没带 X-Request-ID 时补上 gateway 生成的那个，保证下游日志同源
    # （ASGI 把请求头名转成小写，这里按小写判存在，避免同一个头发两遍）
    if "x-request-id" not in {k.lower() for k in headers}:
        headers["X-Request-ID"] = request.state.request_id

    # 补 X-Forwarded-For：下游要靠它拿到**真实**客户端 IP。
    #
    # 不补的话，下游看到的 request.client.host 永远是网关自己 —— 限流和审计
    # 就会把全部用户算成同一个来源，一人一刀就误伤所有人。
    # 已有 XFF 的（客户端自己带的，或上游反代带的）不改写。
    # ⚠️ 这里**覆盖**而不是透传（2026-10-21）。
    #
    # 原来是有就透传、无才补 —— 那等于把"客户端能伪造 XFF"这件事
    # 直接传给了下游：任何人自己发一个 X-Forwarded-For 就能绕过
    # 下游的登录限流（每次换一个假 IP 就行）。
    #
    # 现在由网关自己解析：只有来自**可信代理**（TRUSTED_PROXY_IPS）的
    # XFF 才采信，直连的一律用 peer IP。解析结果**覆盖**掉客户端带的。
    _set_forwarded_for(headers, request)

    client: httpx.AsyncClient = request.app.state.httpx
    try:
        upstream = await client.request(
            request.method,
            f"{route.target_url}{request.url.path}",
            params=request.query_params,
            content=await request.body(),
            headers=headers,
            timeout=_UPSTREAM_TIMEOUT,
        )
    except httpx.RequestError as exc:
        # 超时 / 连不上 / 下游崩了 → 业务信封，不是裸 500。
        # 注意 httpx.TimeoutException 是 RequestError 的子类，这里靠 isinstance 区分。
        return _upstream_error_response(request.url.path, exc)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def proxy_d9(request: Request) -> Response:
    """D9 回调：gateway 侧不强制 JWT —— Authorization 有就透传，没有就不带。

    承宇 2026-09-16 决策：身份判定交给下游 content-service 的
    require_user_optional（已登录用 user_id，未登录用 device-id 头，都没有 → 4001）。
    """
    return await proxy(request, D9_ROUTE)


# 按路由表动态注册（必须在下面的 /api/v1/{path:path} 兜底路由之前 —— Starlette
# 按注册顺序匹配，兜底路由放在后面才不会把精确路由吃掉）。
#
# CP1.7.3：这里早先用 functools.partial(proxy, route=_route) 绑定 route —— fastapi
# 0.141.1 不再解 partial 的签名，会把 Route 的字段（method / path / target_service）
# 当成 request body 模型去校验，POST 带 JSON body 一律 422（"missing field 'method'"）。
# 改用闭包工厂：注册的是普通 async 函数，签名里只剩 request，无 partial 解析问题。
def make_proxy(route: Route):
    """闭包工厂：捕获 route 变量，避开 functools.partial 的 fastapi 解析问题。"""

    async def _proxy(request: Request) -> Response:
        return await proxy(request, route)

    return _proxy


for _route in ROUTES:
    if _route.special == "d9":
        continue  # D9 单独挂（见下）：不要求登录态
    app.add_api_route(
        _route.path,
        make_proxy(_route),  # ← 普通函数，无 partial 解析问题
        methods=[_route.method],
        name=f"proxy_{_route.method.lower()}_{_route.path}",
    )

app.add_api_route(D9_ROUTE.path, proxy_d9, methods=["POST"], name="proxy_d9")

app.include_router(event_router)


def _resolve_target(path: str) -> str | None:
    """根据第一段路径决定下游 base url。"""
    # CP4.7.1: articles/{id}/distill 优先走 ai-service（fallback 误判会走 content-service → 404）
    if path.startswith("articles/") and "/distill" in path:
        return settings.ai_service_url
    segment = path.split("/", 1)[0]
    if segment in ("user", "subscription"):
        return settings.user_service_url
    if segment in ("articles", "tags", "callback"):
        return settings.content_service_url
    if segment == "distill":
        return settings.ai_service_url
    # 2026-10-22：`users`（复数）原来不在白名单里 →
    # `/api/v1/users/me/quota/reset-monthly` 这种路径在网关直接 404
    # 「接口不存在」，而实现其实就在 user-service（实测：网关 404，
    # 直连 user-service 才 401）。这是**同一类事故的第四例** ——
    # config.py 里注释记着前三次（notifications/mark-read、
    # admin/export/tags.csv、admin/llm/*）。
    #
    # 与其再等第五次，不如把兜底的白名单补齐：显式路由表**先**匹配，
    # 命中不了的才走这里，所以把 `users` 划给 user-service 不会抢路由。
    # 兜底猜对 = 端点能通；猜不到 = 404。宁可通到下游让它自己答 404/401，
    # 也不要网关替下游撒谎说"接口不存在"。
    if segment == "users":
        return settings.user_service_url
    return None


@app.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_fallback(request: Request, path: str):
    """路由表没命中的兜底：按第一段路径猜下游（CP1.4/1.5 行为，逐步被路由表取代）。"""
    target_base = _resolve_target(path)
    if target_base is None:
        # 必须走业务信封，不能返回裸 Response。
        #
        # 原来这里直接 `Response(404, b'{"detail":"no downstream route"}')`，
        # 绕过了 register_exception_handlers 装的所有处理器。后果：
        # 安卓 `ApiEnvelope.parseEnvelope()` 期望 {code, message, data}，
        # 解不出来就把 bizCode 置 0，于是 ErrorMessages.kt 里
        # isAudioNotReady(40400) / isQuotaExceeded(3001) / isAuthExpired(40100)
        # / isCaptureFetchFailed(2001|2002) **四个分支全部落空**，
        # 最后落到 404 的兜底文案「内容不存在或已删除」。
        #
        # 也就是说：**端点没上线**被误报成**文章被删了** —— 和 admin-web 侧
        # client.ts 的 isEndpointMissing() 专门区分 404/501 的动机正好相反。
        #
        # 走 BizException 拿 40400，与全局 _STATUS_CODE_MAP 的既有约定一致。
        return JSONResponse(
            status_code=404,
            content={"code": 40400, "message": "接口不存在", "data": None},
        )

    url = f"{target_base}/api/v1/{path}"
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    # 同 proxy()：补 X-Request-ID，让下游日志与网关日志串得上号。
    #
    # 2026-10-22：兜底路径原来只补 XFF、不补请求 ID → 走兜底的端点
    # （当时是剪藏与重试这两个最核心的写端点）在下游日志里是「孤儿」，
    # 排查「剪藏失败要回溯扣费与蒸馏派发」时两边串不起来。
    # 显式路由早就补了 ID，兜底却不补 —— 同一个网关两套链路串联口径。
    if "x-request-id" not in {k.lower() for k in headers}:
        headers["X-Request-ID"] = request.state.request_id
    # 同 proxy()：补 X-Forwarded-For，让下游拿得到真实客户端 IP（限流/审计要用）。
    # 同上：覆盖而非透传（原因见 proxy() 里的说明）
    _set_forwarded_for(headers, request)

    client = request.app.state.httpx
    try:
        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            content=body,
            headers=headers,
            timeout=_UPSTREAM_TIMEOUT,
        )
    except httpx.RequestError as exc:
        # 与 proxy() 同理：兜底路由也不能吐裸 500。
        # path 形如 "articles" / "articles/add"（没有前导 /api/v1），
        # 所以这里拼回完整路径再判是不是剪藏。
        return _upstream_error_response(f"/api/v1/{path}", exc)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
