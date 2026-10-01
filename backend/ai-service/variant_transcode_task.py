"""音频变体转码任务（CP7.3.0，2026-10-02 新增）。

## 为什么要有这个任务

`GET /api/v1/tts/variants`（用户点开文章详情页时调）原来会在**读请求里同步**
跑 ffmpeg 转 30 分钟音频。而安卓端超时上限 65s，于是：

- 地铁隧道里打开一篇只有主档的文章 → 转码跑不完 → 客户端抛异常 →
  选档整条失败。**弱网场景反而比强网更糟**，这不合理。
- 强网下也只是「点了详情页要干等几十秒」。

## 现在的口径

读接口发现缺档时：返回 `available=false`（主档立刻可用）+ 把转码丢进
arq 队列。用户当下能播就行，低码率转好了下次就有了。
这正是产品化方案 §5 决策 2 的选项 B「按需转码」该有的样子。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

log = logging.getLogger("ai-service.variant_transcode")


async def variant_transcode_task(
    ctx: dict,
    article_id: str,
    bitrate: int,
) -> dict:
    """Arq worker 入口：为一篇文章生成指定码率的变体。

    幂等：表里已有就直接返回（重复入队不会重复转码）。
    失败返回 {"ok": False} 而不是抛 —— 抛了 Arq 会重试，但转码失败重试
    意义不大（源文件可能真有问题），让调用方自己决定。
    """

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import ArticleAudioVariant, DistilledArticle

    from distill.audio_variant_service import get_variant_service

    try:
        async with AsyncSessionLocal() as db:
            existing = await db.scalar(
                select(ArticleAudioVariant).where(
                    ArticleAudioVariant.distilled_article_id == article_id,
                    ArticleAudioVariant.bitrate == bitrate,
                )
            )
            if existing is not None:
                return {"ok": True, "skipped": True, "bitrate": bitrate}

            da = await db.scalar(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            if da is None:
                return {"ok": False, "reason": "distilled_article_not_found"}

            svc = get_variant_service()
            row = await svc.ensure_variant(db, da, bitrate)
            return {"ok": row is not None, "bitrate": bitrate}
    except Exception as exc:
        log.warning(
            "variant_transcode_failed article=%s bitrate=%s err=%s", article_id, bitrate, exc
        )
        return {"ok": False, "bitrate": bitrate, "error": str(exc)}
