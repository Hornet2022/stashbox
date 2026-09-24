"""文章硬删除共享逻辑（CP-DELETE）。

main.py（用户端 DELETE /api/v1/articles/{id}）与 admin_router.py
（admin DELETE /api/v1/admin/articles/{id}）共用，抽出来避免
admin_router ← main 的循环导入（main 已 include admin_router）。

删除顺序按 FK 依赖（DB 实测：articles 子表中仅 push_notifications 为
ON DELETE CASCADE，distilled_articles / favorites / later_listens /
listening_statuses / feedback_v2 均 NO ACTION，必须应用层按序清）：

    1. favorites / later_listens / listening_statuses 按 article_id 删
    2. distillation_evaluations.task_id 置 NULL（**评分保留**，仅断引用；
       与 feedback_v2.article_id 的取舍一致 —— 评分是画像训练资产）
    3. article_audio_variants 按 distilled_article_id 删（派生数据，
       母体没了变体无意义；DB 亦已 0030 加 CASCADE 兜底）
    4. distilled_articles 按 article_id 删（蒸馏任务 + 音频元数据）
    5. feedback_v2.article_id 置 NULL（反馈记录保留，仅断引用）
    6. articles 本体删除
    —— push_notifications 由 DB CASCADE 自动清；feedback（v1 埋点表）
    article_id 列无 FK，作为孤儿分析数据保留。
    7.（commit 成功后）磁盘/OSS 音频文件删除 —— best-effort，失败只记
    warning 不回滚：宁可留孤儿文件等 GC，不可音频没了文章还在。
    8. Redis 文章详情缓存 invalidate_article。

> 历史坑（0030 修复）：0024/0026 建表时 FK 未声明 ondelete，步骤 2/3
> 缺失导致删任何「有评分或有变体」的文章都 500。应用层现显式处理，
> DB 级联作为兜底。

quota 不返还：提交时扣的配额视为已消耗（蒸馏 LLM/TTS 成本已发生）。
"""

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common import cache_service
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import (
    Article,
    ArticleAudioVariant,
    DistilledArticle,
    DistillationEvaluation,
    Favorite,
    FeedbackV2,
    LaterListen,
    ListeningStatus,
)

log = get_logger("content-service.purge")

# 与 pipeline._save_audio 的 key 模板 audio/{article_id}.{format} 对齐；
# 历史数据扩展名不一（edge→m4a、indextts/local→wav），全部候选扫一遍。
_AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".ogg", ".aac")

# 多码率变体文件（CP7.3.0 audio_variant_generator）：audio/{article_id}.{bitrate}k.{ext}
# 与主档分开命名，delete_article_audio 需额外扫这些组合，否则删文章后变体文件成孤儿。
_VARIANT_BITRATES = (128, 96, 64)


async def delete_article_audio(article_id: str) -> None:
    """删除文章的蒸馏音频文件 + 多码率变体（幂等、best-effort，异常只记日志）。"""
    try:
        from stashbox.backend.app.services.storage import get_storage

        storage = get_storage()
    except Exception as exc:  # storage 未配置等：不影响 DB 删除结果
        log.warning("audio_storage_unavailable", article_id=article_id, error=str(exc))
        return

    # 主档：audio/{article_id}.{ext}
    keys = [f"audio/{article_id}{ext}" for ext in _AUDIO_EXTS]
    # 变体：audio/{article_id}.{bitrate}k.{ext}
    keys += [
        f"audio/{article_id}.{bitrate}k{ext}"
        for bitrate in _VARIANT_BITRATES
        for ext in _AUDIO_EXTS
    ]

    for key in keys:
        try:
            if await storage.exists(key):
                await storage.delete(key)
                log.info("audio_file_deleted", article_id=article_id, key=key)
        except Exception as exc:
            log.warning("audio_file_delete_failed", article_id=article_id, key=key, error=str(exc))


async def purge_article(db: AsyncSession, article_id: str) -> Article:
    """硬删除文章 + 关联数据（不含音频文件与缓存，见函数序言）。

    调用方负责最终 commit；本函数内只做 ORM delete/update + flush。
    返回被删的 Article 实例（commit 后仅可用于读已加载属性，如 user_id）。
    """
    art = (await db.execute(select(Article).where(Article.id == article_id))).scalar_one_or_none()
    if art is None:
        return None  # type: ignore[return-value]

    await db.execute(delete(Favorite).where(Favorite.article_id == article_id))
    await db.execute(delete(LaterListen).where(LaterListen.article_id == article_id))
    await db.execute(delete(ListeningStatus).where(ListeningStatus.article_id == article_id))

    # 蒸馏产物的下游引用：先断评分引用 + 清变体，再删产物本体。
    # （0030 前这里的缺失会让删除 500 —— 见模块 docstring 的「历史坑」）
    distilled_ids = (
        (
            await db.execute(
                select(DistilledArticle.id).where(DistilledArticle.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    if distilled_ids:
        # 评分保留，仅断引用（task_id 0030 起 nullable + ON DELETE SET NULL）
        await db.execute(
            update(DistillationEvaluation)
            .where(DistillationEvaluation.task_id.in_(distilled_ids))
            .values(task_id=None)
        )
        # 多码率变体是派生数据，随产物清理
        await db.execute(
            delete(ArticleAudioVariant).where(
                ArticleAudioVariant.distilled_article_id.in_(distilled_ids)
            )
        )

    await db.execute(delete(DistilledArticle).where(DistilledArticle.article_id == article_id))
    await db.execute(
        update(FeedbackV2).where(FeedbackV2.article_id == article_id).values(article_id=None)
    )
    await db.delete(art)
    await db.flush()
    return art


async def finish_purge(article_id: str, owner_user_id: int) -> None:
    """commit 成功后的收尾：失效缓存 + 清音频文件。整体 best-effort。"""
    try:
        await cache_service.invalidate_article(article_id)
    except Exception as exc:
        log.warning("invalidate_article_failed", article_id=article_id, error=str(exc))
    try:
        await cache_service.invalidate_pending(owner_user_id)
    except Exception as exc:
        log.warning("invalidate_pending_failed", user_id=owner_user_id, error=str(exc))
    await delete_article_audio(article_id)
