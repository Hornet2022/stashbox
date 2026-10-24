"""订阅闭环的**读侧授权**（2026-10-24 产品拍板后）。

## 产品决策（本文件的权威依据）

> 「admin 录入的内容……应该在用户 app 的订阅功能中形成闭环，
> **订阅了分类就可以看到，没订阅就看不到**。」

## 为什么这个文件存在（一个真实的断链）

订阅流（`GET /api/v1/subscriptions/feed`）与订阅推送**本来就不按 owner 过滤**
—— 它们按**分类**分发。所以运营录入的内容（归属 `user_id=0`）天然会出现在
订阅者的流里。

但**读侧端点只认属主**：详情 / 音频 / 进度 / 听完 / 评分全走 `_get_owned`，
非属主一律 403 ⇒ **闭环断在最后一步**：订阅流里明明列出了这篇文章，
用户点进去却是「无权访问」，音频也拿不到。

⇒ 这一层放行**只认分类的订阅者**读/听/记录自己的状态。

## 三个必须守住的边界

1. **没订阅就看不到** —— 非属主且未订阅 → 403（与订阅流的筛选同源）；
2. **只放行「消费」类操作** —— 读稿、听音频、记自己的进度/评分/收藏；
   **删除 / 重试蒸馏 / 稍后读**是属主的私有操作；
3. **`skip` 与 `mark-listened` 仍然 403** ——
   它们写的是**共享列**（`articles.skip` / `articles.status`），
   订阅者改一次会影响**所有人**（包括运营录的内容的所有订阅者）。
"""

from __future__ import annotations

import pytest

from helpers import content_main, new_user
from stashbox.backend.common.models import Article, DistilledArticle, Tag, TagSubscription
from stashbox.backend.common.database import AsyncSessionLocal


# ---------------------------------------------------------------------------
# 准备：造出「一篇别人的、已就绪、有分类的文章」+「一个订阅了该分类的用户」
# ---------------------------------------------------------------------------
async def _seed_ready_article(
    *,
    owner_id: int,
    tag_name: str = "科技",
    status: str = "done",
    with_audio: bool = True,
    article_tags: list[str] | None = None,
) -> str:
    """造一篇「属于别人」的已就绪文章，返回 article_id。"""
    import uuid

    art_id = "art_" + uuid.uuid4().hex[:24]
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=art_id,
                user_id=owner_id,
                url=f"https://example.com/{art_id}",
                title=f"别人家的文章 {art_id[:8]}",
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
                status=status,
                script_text="稿子正文" * 40,
                audio_url="https://oss.example.com/a.mp3" if with_audio else None,
                duration_sec=60,
                tags=article_tags if article_tags is not None else [tag_name],
            )
        )
        await s.commit()
    return art_id


def select_tag_by_name(name: str):
    from sqlalchemy import select as _select

    return _select(Tag).where(Tag.name == name)


async def _subscribe(user_id: int, tag_name: str = "科技") -> None:
    async with AsyncSessionLocal() as s:
        tag = await s.scalar(select_tag_by_name(tag_name))
        assert tag is not None, f"tags 表里没有 {tag_name}"
        s.add(TagSubscription(user_id=user_id, tag_id=tag.id))
        await s.commit()


def _override_auth(dep_name: str, mock_user: dict):
    dep = getattr(content_main, dep_name)

    class _Ctx:
        def __enter__(self):
            content_main.app.dependency_overrides[dep] = lambda: mock_user
            return self

        def __exit__(self, *exc):
            content_main.app.dependency_overrides.pop(dep, None)

    return _Ctx()


async def _get_detail(article_id: str, user_id: int) -> int:
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_user", {"id": user_id, "sub": str(user_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/articles/{article_id}")
            return resp.status_code


async def _get_audio(article_id: str, user_id: int) -> int:
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_user", {"id": user_id, "sub": str(user_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/articles/{article_id}/audio-url")
            return resp.status_code


# ---------------------------------------------------------------------------
# 1. 订阅了 → 能读能听
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_订阅了该分类_能读到别人的文章():
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)
    await _subscribe(sub_id, "科技")

    assert await _get_detail(art_id, sub_id) == 200, "订阅者应能打开订阅到的内容"


@pytest.mark.asyncio
async def test_订阅了该分类_能拿到音频():
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)
    await _subscribe(sub_id, "科技")

    assert await _get_audio(art_id, sub_id) == 200, "订阅者应能拿到音频（否则点了播不了）"


# ---------------------------------------------------------------------------
# 2. 没订阅 → 看不到（403）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_没订阅该分类_打不开():
    owner_id, _ = await new_user()
    other_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)

    assert await _get_detail(art_id, other_id) == 403, "没订阅就不该能打开"


@pytest.mark.asyncio
async def test_订阅了别的分类_也打不开():
    """订阅「财经」看不到「科技」的内容 —— 分类必须**精确**匹配。"""
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id, tag_name="科技")
    await _subscribe(sub_id, "财经")

    assert await _get_detail(art_id, sub_id) == 403


# ---------------------------------------------------------------------------
# 3. 边界：未就绪 / 没分类
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_未就绪的文章_订阅者也打不开():
    """没蒸馏完 = 没有可听的东西 ⇒ 不该出现在订阅流，也不该放行。"""
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id, status="running", with_audio=False)
    await _subscribe(sub_id, "科技")

    assert await _get_detail(art_id, sub_id) == 403


@pytest.mark.asyncio
async def test_没有分类的文章_订阅者也打不开():
    """没有分类 = 进不了任何人的订阅流（死内容），更不该被放行。"""
    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id, article_tags=[])
    await _subscribe(sub_id, "科技")

    assert await _get_detail(art_id, sub_id) == 403


# ---------------------------------------------------------------------------
# 4. 属主路径一个字没变
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_属主读自己的文章_不受影响():
    owner_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)

    assert await _get_detail(art_id, owner_id) == 200


# ---------------------------------------------------------------------------
# 5. 共享列仍然属主专属（订阅者改一次会影响所有人）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_skip_仍然只有属主能改():
    """`skip` 写的是**共享列** `articles.skip` ⇒ 订阅者改了会影响所有人。"""
    from httpx import ASGITransport, AsyncClient

    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)
    await _subscribe(sub_id, "科技")

    with _override_auth("require_user", {"id": sub_id, "sub": str(sub_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/v1/articles/{art_id}/skip", json={"reason": "不感兴趣"})
    assert resp.status_code == 403, "skip 改的是共享列，不能给订阅者"


@pytest.mark.asyncio
async def test_mark_listened_仍然只有属主能改():
    """`mark-listened` 写的是**共享列** `articles.status` ⇒ 同上。"""
    from httpx import ASGITransport, AsyncClient

    owner_id, _ = await new_user()
    sub_id, _ = await new_user()
    art_id = await _seed_ready_article(owner_id=owner_id)
    await _subscribe(sub_id, "科技")

    with _override_auth("require_user", {"id": sub_id, "sub": str(sub_id)}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/v1/articles/{art_id}/mark-listened")
    assert resp.status_code == 403, "mark-listened 改的是共享列，不能给订阅者"


# ---------------------------------------------------------------------------
# 6. 运营录入的分类（manual_tags）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_运营录入的分类写进_articles_manual_tags():
    """运营指定的分类必须落到 articles.manual_tags（蒸馏时优先用它）。"""
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", {"id": 9001, "tier": "admin"}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={
                    "file": ("运营分类.md", ("运营分类专用正文" * 60).encode(), "text/markdown")
                },
                data={"tags": ["科技"]},
            )
    assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, resp.json()["article_id"])
        assert art is not None
        assert art.manual_tags == ["科技"]
        # 归属仍是匿名（订阅流按分类分发，不进任何人的个人列表）
        assert art.user_id == 0


@pytest.mark.asyncio
async def test_重复录入同一份文档但换了分类_分类要被更新():
    """同一份文档（内容 hash 相同 ⇒ url 相同 ⇒ 复用同一篇）再次录入并指定新分类。

    为什么锁这条：运营的分类是**分发依据**，静默丢掉它 = 运营选了「财经」、
    内容却还挂在「科技」下，而运营看到的是「创建成功」**且没有任何报错**。
    """
    from httpx import ASGITransport, AsyncClient

    body = ("重复录入专用正文" * 60).encode()

    async def _post(tag: str):
        with _override_auth("require_admin_or_operator", {"id": 9001, "tier": "admin"}):
            transport = ASGITransport(app=content_main.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v1/admin/articles/document",
                    files={"file": ("重复.md", body, "text/markdown")},
                    data={"tags": [tag]},
                )

    r1 = await _post("科技")
    r2 = await _post("财经")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["article_id"] == r2.json()["article_id"], "同一份文档应复用同一篇"

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, r2.json()["article_id"])
        assert art is not None
        assert art.manual_tags == ["财经"], f"分类应被更新，实际 {art.manual_tags}"


@pytest.mark.asyncio
async def test_运营不指定分类时_manual_tags为空_用LLM猜的():
    """不指定 = 沿用 LLM 猜的分类（老行为不变）。"""
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", {"id": 9001, "tier": "admin"}):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("不分类.md", ("不分类专用正文" * 60).encode(), "text/markdown")},
            )
    assert resp.status_code == 200, resp.text
    async with AsyncSessionLocal() as s:
        art = await s.get(Article, resp.json()["article_id"])
        assert art is not None
        assert not art.manual_tags
