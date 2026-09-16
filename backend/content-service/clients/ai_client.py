"""ai-service HTTP 客户端（content-service → ai-service）。

ai-service 的 `POST /api/v1/articles/{id}/distill` 只建任务就返回（mock 流水线在它自己
的 BackgroundTask 里跑），所以本调用本身很快，D9 入口直接 await 拿 task_id。
失败只 log 不抛 —— D9 是用户入口，ai-service 不可用也要先把文章建下来。
"""
import httpx

from stashbox.backend.common.config import settings
from stashbox.backend.common.logging import get_logger

log = get_logger(__name__)


class AIServiceClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0, max_retries: int = 2):
        self.base_url = (base_url or settings.ai_service_url).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    async def trigger_distill(
        self,
        article_id: str,
        auth_token: str | None = None,
        simulate_failure: bool = False,
    ) -> dict | None:
        """触发蒸馏，返回 `{"article_id", "task_id", "status", ...}`，失败返回 None。

        auth_token：ai-service 的 distill 端点挂了 require_user，需带上文章 owner 的 JWT
        （TODO CP1.8+ 换内部服务鉴权）。
        """
        url = f"{self.base_url}/api/v1/articles/{article_id}/distill"
        params = {"simulate_failure": "true"} if simulate_failure else None
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else None

        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, params=params, headers=headers)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                # 4xx/5xx 是确定性结果，重试无意义
                log.error(
                    "ai_service_distill_failed",
                    extra={
                        "article_id": article_id,
                        "status": e.response.status_code,
                        "error": str(e),
                    },
                )
                return None
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == self.max_retries - 1:
                    log.error(
                        "ai_service_distill_failed",
                        extra={"article_id": article_id, "error": str(e)},
                    )
                    return None


_client: AIServiceClient | None = None


def get_ai_client() -> AIServiceClient:
    global _client
    if _client is None:
        _client = AIServiceClient()
    return _client
