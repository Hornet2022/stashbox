"""0041 用户剪藏内容共享闭环（2026-10-26 产品拍板）。

## 为什么有这份测试

共享是订阅闭环的**入流源头**：用户剪藏一篇 → LLM 已给过分类
（`distilled_articles.tags`），但用户可能想把它**额外**公开给**别的**分类
的订阅者。`articles.shared_tags` 就是这份「额外公开」。

关键语义（与 `_effective_tags` 的 docstring 一致）：
- 订阅者能看到的标签 = `da.tags ∪ shared_tags`（**并存不覆盖**，LLM 分类不丢）；
- **属主永远能看自己的内容**，`shared` 只描述「对别人是否可见」；
- 订阅者视角恒为 `shared=false / shared_tags=null`（不泄露属主意愿）；
- `shared_tags` 为空 ⇒ 取消共享（置 NULL）。

## 这里锁住的行为

1. 共享后：订阅了「共享标签」的用户能在订阅流里看到、能读；
2. **没共享**（即使属主有这篇）→ 订阅者看不到；
3. **取消共享** → 订阅者立刻看不到（读侧 403、流里消失）；
4. 合并语义：`da.tags` 里已有分类 + `shared_tags` 额外分类 = 两个订阅者都能看到；
5. 只属主可共享（订阅者调 share 接口 403）；
6. 属主视角能看到 `shared=true / shared_tags`，订阅者视角恒为未共享。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

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


async def _seed_owned_article(owner_id: int, *, da_tags: list[str]) -> str:
    """造一篇「属主」的已就绪文章（da.tags = LLM 分类），返回 article_id。"""
    art_id = "art_" + uuid.uuid4().hex[:24]
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=art_id,
                user_id=owner_id,
                url=f"https://example.com/{art_id}",
                title=f"共享测试文章 {art_id[:8]}",
                source="web",
                status="ready",
                raw_content={"content_text": "正文内容" * 50, "source": "web"},
                favorite=False,
                skip=False,
            )
        )
        s.add(
            DistilledArticle(
                id=f"dst_{art_id[4:]}",
                article_id=art_id,
                status="done",
                script_text="稿子正文" * 40,
                audio_url="https://oss.example.com/a.mp3",
                duration_sec=60,
                tags=da_tags,
            )
        )
        await s.commit()
    return art_id


async def _ensure_tag(name: str, slug: str) -> None:
    """确保 tags 表里有该标签（订阅/共享都按 name 匹配）。"""
    from sqlalchemy import select as _select

    async with AsyncSessionLocal() as s:
        existing = await s.scalar(_select(Tag).where(Tag.name == name))
        if existing is None:
            s.add(Tag(slug=slug, name=name, category="test", is_system=False, creator_id=1))
            await s.commit()


async def _new_user() -> int:
    uid, _ = await new_user()
    return uid


async def _subscribe(user_id: int, tag_name: str) -> None:
    from sqlalchemy import select as _select

    async with AsyncSessionLocal() as s:
        tag = await s.scalar(_select(Tag).where(Tag.name == tag_name))
        assert tag is not None, f"tags 表里没有 {tag_name}"
        s.add(TagSubscription(user_id=user_id, tag_id=tag.id))
        await s.commit()


async def _feed_ids(user_id: int) -> list[str]:
    with _override_auth("require_user", {"id": user_id, "sub": str(user_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/subscriptions/feed")
    assert resp.status_code == 200, resp.text
    return [i["article_id"] for i in resp.json()["items"]]


async def _detail_status(article_id: str, user_id: int) -> int:
    with _override_auth("require_user", {"id": user_id, "sub": str(user_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/articles/{article_id}")
            return resp.status_code


async def _share(article_id: str, user_id: int, tags: list[str]):
    with _override_auth("require_user", {"id": user_id, "sub": str(user_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/articles/{article_id}/share", json={"shared_tags": tags}
            )


@pytest.mark.asyncio
async def test_共享后_订阅共享标签的用户能在流里看到并可读():
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    await _ensure_tag("共享科技", f"share_{uuid.uuid4().hex[:8]}")
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["LLM分类"])
    await _subscribe(sub_id, "共享科技")

    # 未共享前：订阅了「共享科技」的用户看不到
    assert art_id not in await _feed_ids(sub_id), "未共享不该出现在订阅流"
    assert await _detail_status(art_id, sub_id) == 403, "未共享不该能读"

    # 属主共享到「共享科技」
    resp = await _share(art_id, owner_id, ["共享科技"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared"] is True
    assert resp.json()["shared_tags"] == ["共享科技"]

    # 订阅者现在能看到、能读
    assert art_id in await _feed_ids(sub_id), "共享后订阅者应在流里看到"
    assert await _detail_status(art_id, sub_id) == 200, "共享后订阅者应能读"


@pytest.mark.asyncio
async def test_取消共享后_订阅者立刻看不到():
    owner_id = await _new_user()
    sub_id = await _new_user()
    await _ensure_tag("取消共享", f"unshare_{uuid.uuid4().hex[:8]}")
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["LLM分类"])
    await _subscribe(sub_id, "取消共享")

    await _share(art_id, owner_id, ["取消共享"])
    assert art_id in await _feed_ids(sub_id), "前置：共享后应在流里"

    # 取消共享（等价于传空数组）
    resp = await _share(art_id, owner_id, [])
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared"] is False
    assert resp.json()["shared_tags"] is None

    assert art_id not in await _feed_ids(sub_id), "取消共享后订阅流里消失"
    # GET 详情对「非属主且无订阅授权」恒为 403（越权 403 契约，main.py 顶部注释），
    # 与「资源不存在」的 404 有意区分；取消共享后退回这一档。
    assert await _detail_status(art_id, sub_id) == 403, "取消共享后订阅者不能读（越权 403）"


@pytest.mark.asyncio
async def test_合并语义_LLM分类和共享标签两个订阅者都能看到():
    owner_id = await _new_user()
    sub_llm = await _new_user()
    sub_shared = await _new_user()
    await _ensure_tag("共享额外", f"merge_{uuid.uuid4().hex[:8]}")
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["科技"])
    await _subscribe(sub_llm, "科技")
    await _subscribe(sub_shared, "共享额外")

    # 共享到「共享额外」：并存不覆盖 —— 订阅「科技」（LLM 分类）的仍能看到
    await _share(art_id, owner_id, ["共享额外"])

    assert art_id in await _feed_ids(sub_llm), "共享不覆盖 LLM 分类：订阅科技仍可见"
    assert art_id in await _feed_ids(sub_shared), "订阅共享额外标签可见"


@pytest.mark.asyncio
async def test_非属主不能替别人共享():
    owner_id = await _new_user()
    sub_id = await _new_user()
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["科技"])

    resp = await _share(art_id, sub_id, ["科技"])
    assert resp.status_code == 403, "订阅者不能替别人共享"


@pytest.mark.asyncio
async def test_订阅者视角_共享字段恒为未共享():
    """订阅者看到的是别人的内容，「我能不能共享它」无从谈起 ⇒ 不泄露属主意愿。"""
    owner_id = await _new_user()
    sub_id = await _new_user()
    await _ensure_tag("共享查看", f"view_{uuid.uuid4().hex[:8]}")
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["科技"])
    await _subscribe(sub_id, "共享查看")
    await _share(art_id, owner_id, ["共享查看"])

    with _override_auth("require_user", {"id": sub_id, "sub": str(sub_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/articles/{art_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["shared"] is False, "订阅者视角 shared 恒为 false"
    assert body["shared_tags"] is None, "订阅者视角 shared_tags 恒为 null"


@pytest.mark.asyncio
async def test_属主视角_能看到自己的共享状态():
    owner_id = await _new_user()
    art_id = await _seed_owned_article(owner_id=owner_id, da_tags=["科技"])
    await _share(art_id, owner_id, ["科技"])

    with _override_auth("require_user", {"id": owner_id, "sub": str(owner_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/articles/{art_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared"] is True
    assert resp.json()["shared_tags"] == ["科技"]
