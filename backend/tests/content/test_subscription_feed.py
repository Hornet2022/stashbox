"""订阅内容流：GET /api/v1/subscriptions/feed（2026-10-23）。

## 为什么有这个测试

订阅此前**只有「写」没有「读回」**：能订阅，但**没有任何端点**能返回
「我订阅的标签下产生了哪些内容」。而 `GET /api/v1/articles?tag=` 被属主过滤
限死（只返回自己的文章），承载不了「订阅带来的是**别人**的内容」。

加上 agent 路径从不写 `distilled_articles.tags`（推送的唯一输入），
整条链路上「订阅」= 一个**永远没有回报的开关** —— 内测反馈说的就是它。

## 这里锁住的行为

1. 没订阅任何标签 → 空列表（**不是 404**，那是合法状态）；
2. 订阅了 → 返回该标签下**已就绪**的内容；
3. **别人的**内容也要出现（这正是订阅的意义，也是与文章列表最大的区别）；
4. 没就绪的（status != done / 没音频）不出现 —— 推给用户点开是空的；
5. 不订阅的标签下的内容不出现。
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from helpers import content_main, new_user
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, Tag, TagSubscription


def _override_auth(dep_name: str, mock_user: dict):
    dep = getattr(content_main, dep_name)

    class _Ctx:
        def __enter__(self):
            content_main.app.dependency_overrides[dep] = lambda: mock_user
            return self

        def __exit__(self, *exc):
            content_main.app.dependency_overrides.pop(dep, None)

    return _Ctx()


@pytest.mark.asyncio
async def test_feed_返回订阅标签下已就绪的内容():
    slug = f"feed_tag_{uuid.uuid4().hex[:12]}"
    tag_name = f"订阅测试标签{uuid.uuid4().hex[:6]}"
    user_id, _tok = await new_user()
    other_user_id, _ = await new_user()

    async with AsyncSessionLocal() as s:
        tag = Tag(slug=slug, name=tag_name, category="test", is_system=False, creator_id=1)
        s.add(tag)
        await s.commit()

    # 造一篇「别人」的、已就绪、带该标签的文章
    async with AsyncSessionLocal() as s:
        art = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=other_user_id,
            url="https://example.com/x",
            title="订阅来的文章",
            source="wechat",
        )
        s.add(art)
        da = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=art.id,
            status="done",
            script_text="正文",
            audio_url="https://example.com/a.mp3",
            duration_sec=120,
            tags=[tag_name],
        )
        s.add(da)
        await s.commit()
        article_id = art.id

    # 造一篇「还没好」的文章（同标签）—— 不该出现在 feed 里
    async with AsyncSessionLocal() as s:
        art2 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=other_user_id,
            url="https://example.com/y",
            title="还在蒸馏",
            source="wechat",
        )
        s.add(art2)
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=art2.id,
                status="running",
                tags=[tag_name],
            )
        )
        await s.commit()
        pending_article_id = art2.id

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 没订阅 → 空列表（不是 404）
            resp = await client.get("/api/v1/subscriptions/feed")
            assert resp.status_code == 200, resp.text
            assert resp.json()["items"] == []

            # 订阅
            resp = await client.post(f"/api/v1/tags/{slug}/subscribe")
            assert resp.status_code == 200, resp.text

            resp = await client.get("/api/v1/subscriptions/feed")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            ids = [i["article_id"] for i in data["items"]]
            assert article_id in ids, f"订阅标签下已就绪的文章没出现：{data}"
            assert pending_article_id not in ids, "还没就绪的文章不该出现（点开是空的）"
            assert data["total"] >= 1
            assert tag_name in data["tags"]

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(
            delete(DistilledArticle).where(
                DistilledArticle.article_id.in_([article_id, pending_article_id])
            )
        )
        await s.execute(delete(Article).where(Article.id.in_([article_id, pending_article_id])))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_feed_不包含未订阅标签的内容():
    slug_a = f"feed_a_{uuid.uuid4().hex[:12]}"
    slug_b = f"feed_b_{uuid.uuid4().hex[:12]}"
    name_a = f"未订阅{uuid.uuid4().hex[:6]}"
    name_b = f"已订阅{uuid.uuid4().hex[:6]}"
    user_id, _tok = await new_user()
    other_user_id, _ = await new_user()

    async with AsyncSessionLocal() as s:
        ta = Tag(slug=slug_a, name=name_a, category="test", is_system=False, creator_id=1)
        tb = Tag(slug=slug_b, name=name_b, category="test", is_system=False, creator_id=1)
        s.add_all([ta, tb])
        await s.commit()

    async with AsyncSessionLocal() as s:
        arts = []
        for name in (name_a, name_b):
            art = Article(
                id=f"art_{uuid.uuid4().hex[:24]}",
                user_id=other_user_id,
                url="https://example.com/x",
                title=f"属于{name}",
                source="wechat",
            )
            s.add(art)
            s.add(
                DistilledArticle(
                    id=f"dst_{uuid.uuid4().hex[:24]}",
                    article_id=art.id,
                    status="done",
                    audio_url="https://example.com/a.mp3",
                    duration_sec=60,
                    tags=[name],
                )
            )
            arts.append(art.id)
        await s.commit()
        art_a_id, art_b_id = arts

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(f"/api/v1/tags/{slug_b}/subscribe")
            resp = await client.get("/api/v1/subscriptions/feed")
            assert resp.status_code == 200, resp.text
            ids = [i["article_id"] for i in resp.json()["items"]]
            assert art_b_id in ids
            assert art_a_id not in ids, "没订阅的标签下的内容不该出现"

    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(
            delete(DistilledArticle).where(DistilledArticle.article_id.in_([art_a_id, art_b_id]))
        )
        await s.execute(delete(Article).where(Article.id.in_([art_a_id, art_b_id])))
        await s.execute(delete(Tag).where(Tag.slug.in_([slug_a, slug_b])))
        await s.commit()
