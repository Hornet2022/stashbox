"""ai-service → content-service 的**内部调用**（补配图图注）。

## 为什么识图归 content-service 做，ai-service 只发一个 HTTP 请求

1. **图要下载** —— 下载要走 content-service 那套现成的防护
   （SSRF 校验、按 host 节流、池化 client + 按画像的 header）。
   搬到 ai-service 就是**复制第二份**，两份迟早各自漂移。
2. **图注要写回 `articles.raw_content`** —— 那张表虽然两个服务都能读，
   但 content-service 是它的"主人"（剪藏时它写的）。让 ai-service 直写
   等于绕过服务边界，出问题时连"谁改的"都说不清。

⇒ ai-service 只负责**在读正文之前问一句**："这篇的图注补好了吗？"

## 为什么这个调用允许失败

识图失败只意味着「这篇没有图表信息」，正文照常能蒸馏。
⇒ 这里的失败**一律降级成 0**（记日志，不抛），绝不因为补图注失败
   就让整篇蒸馏失败 —— 那等于"为了可有可无的图表，把整篇文章的稿子也赔进去"。

调用方（`tasks/distill_task.py`）还有第二道兜底：正文里残留的占位符
会被 `common.media_placeholders.strip_placeholders` 剥掉，
**绝不让 `\x00IMG3\x00` 这种东西进 LLM 的 prompt**。
"""

from __future__ import annotations

import logging

import httpx

from stashbox.backend.common.config import settings

log = logging.getLogger("stashbox.ai.content_client")

#: 补图注请求的超时（秒）。
#:
#: 比 content-service 那边的**总预算**（90s）宽一点：
#: 端点内部有 90s 的认图预算，外层还得留网络往返的余量。
#: 远小于 arq 的任务墙钟（1800s），不会把任务拖到超时。
CAPTION_FILL_TIMEOUT = 120.0


def _endpoint(article_id: str) -> str:
    base = (settings.content_service_url or "").rstrip("/")
    return f"{base}/api/v1/internal/articles/{article_id}/captions"


async def fill_article_captions(
    article_id: str,
    *,
    timeout: float = CAPTION_FILL_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """给文章补配图图注 → 返回**补上的图注条数**（失败/没有图都是 0）。

    Args:
        article_id: 文章 id。
        timeout: 整次请求的超时（秒）。
        transport: 测试注入点（`httpx.MockTransport`）；生产传 None。

    ⚠️ 本函数**不抛异常**：所有失败都降级成 0 + 日志。
    理由见模块文档（补图注失败不该毁掉整篇蒸馏）。
    """
    secret = settings.callback_shared_secret
    if not secret:
        # fail-closed 的同一套逻辑：没配密钥就别调（调了也是 503）。
        log.warning(
            "caption_fill_skipped_no_secret",
            extra={"article_id": article_id, "reason": "CALLBACK_SHARED_SECRET 未配置"},
        )
        return 0

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.post(
                _endpoint(article_id),
                headers={"X-Callback-Secret": secret},
            )
    except Exception as exc:  # noqa: BLE001 —— 补图注失败不该毁掉整篇蒸馏
        log.warning(
            "caption_fill_request_failed",
            extra={"article_id": article_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return 0

    if resp.status_code != 200:
        log.warning(
            "caption_fill_bad_status",
            extra={"article_id": article_id, "status": resp.status_code},
        )
        return 0

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 响应不是 JSON 也只是"没补上"
        log.warning(
            "caption_fill_bad_payload",
            extra={"article_id": article_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return 0

    count = int(payload.get("captions") or 0)
    log.info(
        "caption_fill_done",
        extra={
            "article_id": article_id,
            "captions": count,
            "images": payload.get("images"),
            "changed": payload.get("changed"),
        },
    )
    return count


__all__ = ["CAPTION_FILL_TIMEOUT", "fill_article_captions"]
