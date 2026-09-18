"""
api-gateway 路由表（CP1.7.1）。

把「path → 下游服务」的映射从 main.py 抽出来：
  - main.py 只负责按表动态注册 + 统一转发，不再散落 if/elif
  - CP1.8+ 接 Nacos / 配置中心时，只需换掉本模块的 ROUTES 装载方式

下游 base url 走 Settings（env 可覆盖：CONTENT_SERVICE_URL 等），
本地 dev 由 run_dev.sh 导出 localhost:810x，容器环境则是 docker 服务名。
"""
from dataclasses import dataclass

from stashbox.backend.common.config import settings


@dataclass(frozen=True)
class Route:
    """一条代理路由。

    auth_required=False 表示 gateway 侧不强制 JWT（如 D9 回调），
    但最终是否放行由下游自己判断（content-service 的 require_user_optional）。
    service-to-service 内部鉴权留 CP1.8+，本期 gateway 只做透传。
    """

    method: str
    path: str
    target_service: str  # content-service / user-service / ai-service
    target_url: str
    auth_required: bool = True
    special: str | None = None  # "d9"：D9 callback，不要求登录态


def _url(service: str) -> str:
    return {
        "user-service": settings.user_service_url,
        "content-service": settings.content_service_url,
        "ai-service": settings.ai_service_url,
    }[service]


# 既有路由（CP1.4/1.5）。顺序有意义：字面量路径（pending/listened）要在
# 带路径参数（{article_id}）之前，否则会被参数路由先吃掉。
ROUTES: list[Route] = [
    Route("POST", "/api/v1/auth/wechat-login", "user-service", _url("user-service")),
    Route("POST", "/api/v1/auth/refresh-token", "user-service", _url("user-service")),
    Route("GET", "/api/v1/user", "user-service", _url("user-service")),
    Route("POST", "/api/v1/articles/add", "content-service", _url("content-service")),
    Route("GET", "/api/v1/articles/pending", "content-service", _url("content-service")),
    Route("GET", "/api/v1/articles/listened", "content-service", _url("content-service")),
    Route(
        "GET", "/api/v1/articles/{article_id}", "content-service", _url("content-service")
    ),
    Route(
        "POST",
        "/api/v1/articles/{article_id}/mark-listened",
        "content-service",
        _url("content-service"),
    ),
    Route(
        "POST",
        "/api/v1/articles/{article_id}/favorite",
        "content-service",
        _url("content-service"),
    ),
    Route(
        "POST",
        "/api/v1/articles/{article_id}/skip",
        "content-service",
        _url("content-service"),
    ),
    # CP5.5 收藏 + 稍后听
    Route("GET", "/api/v1/favorites", "content-service", _url("content-service")),
    Route("GET", "/api/v1/favorites/folders", "content-service", _url("content-service")),
    Route("POST", "/api/v1/articles/{article_id}/favorites", "content-service", _url("content-service")),
    Route("PATCH", "/api/v1/favorites/{favorite_id}", "content-service", _url("content-service")),
    Route("DELETE", "/api/v1/favorites/{favorite_id}", "content-service", _url("content-service")),
    Route("GET", "/api/v1/later-listens", "content-service", _url("content-service")),
    Route("POST", "/api/v1/articles/{article_id}/snooze", "content-service", _url("content-service")),
    Route("DELETE", "/api/v1/articles/{article_id}/snooze", "content-service", _url("content-service")),
    Route("GET", "/api/v1/tags", "content-service", _url("content-service")),
    Route("POST", "/api/v1/distill/start", "ai-service", _url("ai-service")),
    Route("GET", "/api/v1/distill/{task_id}", "ai-service", _url("ai-service")),
    # CP4.7.1: articles/{id}/distill 显式路由到 ai-service（fallback 会错误地走到 content-service）
    Route(
        "POST",
        "/api/v1/articles/{article_id}/distill",
        "ai-service",
        _url("ai-service"),
    ),
]

# CP1.7.1 新增：D9 callback（不要求登录态）+ status + audio-url
ROUTES += [
    Route(
        "POST",
        "/api/v1/callback/d9-add-article",
        "content-service",
        _url("content-service"),
        auth_required=False,
        special="d9",
    ),
    Route(
        "GET",
        "/api/v1/articles/{article_id}/status",
        "content-service",
        _url("content-service"),
    ),
    Route(
        "GET",
        "/api/v1/articles/{article_id}/audio-url",
        "content-service",
        _url("content-service"),
    ),
    # CP5.5-A3 反馈分类
    Route("POST", "/api/v1/feedback-v2", "content-service", _url("content-service")),
    Route("GET", "/api/v1/feedback-v2", "content-service", _url("content-service")),
    # CP-ADMIN: admin 后台路由（admin-web 通过这些端点运营）
    Route("POST", "/api/v1/admin/auth/login", "user-service", _url("user-service")),
    Route("GET", "/api/v1/admin/users", "user-service", _url("user-service")),
    Route("POST", "/api/v1/admin/users/{user_id}/quota-adjust", "user-service", _url("user-service")),
    Route("POST", "/api/v1/admin/articles/{article_id}/force-retry", "content-service", _url("content-service")),
    Route("POST", "/api/v1/admin/audio/{audio_id}/invalidate", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/audit-log", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/stats", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/export/users.csv", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/export/articles.csv", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/export/feedback.csv", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/export/audit-log.csv", "content-service", _url("content-service")),
    Route("GET", "/api/v1/admin/export/subscriptions.csv", "content-service", _url("content-service")),
    # admin-web 用的 POST /api/v1/tags + GET /api/v1/notifications（CP3.6-A2 + CP5.4a）
    Route("POST", "/api/v1/tags", "content-service", _url("content-service")),
    Route("GET", "/api/v1/notifications", "user-service", _url("user-service")),
]


D9_ROUTE: Route = next(r for r in ROUTES if r.special == "d9")
