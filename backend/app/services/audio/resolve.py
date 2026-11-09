"""「这篇文章当前的音频 key 是哪个」的**唯一**查询入口。

与 `stale_key.py` 分开的原因：那一份是**纯函数**（不碰 DB ⇒ 单测不需要数据库），
这一份负责查库。分开是为了让"判据"和"取数"各自可测。

真相源只有一个：`distilled_articles.audio_url`（蒸馏产物行）。
`articles.audio_url` 是文章主档上的冗余字段（CP9.x 早期遗留），
只在蒸馏行缺失时作为兜底 —— 判定逻辑见 [current_audio_key] 的注释。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.app.services.audio.stale_key import key_from_audio_url
from stashbox.backend.common.models.article import Article
from stashbox.backend.common.models.distilled_article import DistilledArticle


async def current_audio_key(session: AsyncSession, article_id: str) -> str | None:
    """查这篇文章**当前**的音频存储 key（`audio/xxx.mp3`）；没有则 None。

    为什么先查蒸馏行而不是文章主档：
    音频是蒸馏的产物，重新蒸馏会换 key，而**只有蒸馏行会被更新**
    （主档的 audio_url 在 CP9.x 之后基本不再维护）。
    ⇒ 蒸馏行是"当前音频"的真相源，主档只是兜底。

    为什么按 updated_at 倒序取第一条：
    理论上 article_id → 蒸馏行是一对一，但历史数据里可能有多行
    （重跑留下的旧行）。取**最新更新**的那行 ⇒ 与"用户现在应该听到什么"一致。
    """
    stmt = (
        select(DistilledArticle.audio_url)
        .where(DistilledArticle.article_id == article_id)
        .order_by(DistilledArticle.updated_at.desc())
        .limit(1)
    )
    url = (await session.execute(stmt)).scalar_one_or_none()
    key = key_from_audio_url(url)
    if key:
        return key

    # 兜底：蒸馏行缺失（老数据 / 音频生成失败的残留）时看文章主档。
    stmt2 = select(Article.audio_url).where(Article.id == article_id).limit(1)
    url2 = (await session.execute(stmt2)).scalar_one_or_none()
    return key_from_audio_url(url2)
