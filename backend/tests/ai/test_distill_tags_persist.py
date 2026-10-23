"""agent 路径必须把**主题标签**落到 distilled_articles.tags（2026-10-23）。

## 为什么这条是订阅闭环的地基

`_trigger_subscription_pushes` 的第一行就是：

    if not da or not da.tags:
        return 0

也就是说 **`distilled_articles.tags` 是订阅推送的唯一输入**。而 agent 路径
（生产真正在跑的那条）此前：

* `runner.py` 里 `"rewrite_tags": []` —— **硬编码的空列表**；
* `_persist_agent_final` 里**从不写** `da.tags`。

结果：生产 `distilled_articles.tags` 实测 **25 行全空**，
`push_notifications` 实测 **0 行** —— 订阅推送**一次都没产生过**。

## 这里锁住的行为

1. `final` 里有 `rewrite_tags` → 写进 `da.tags`；
2. 只保留 **tags 表里真实存在**的名字（推送是 `Tag.name.in_(da.tags)`
   精确匹配，近似词写进去只是垃圾）；
3. 全部都不认识 → 写空列表 + 记 warning（否则「推送没来」又查不到原因）。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from stashbox.backend.common.models import User

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, Tag


@pytest.mark.asyncio
async def _new_user() -> int:
    """建一个测试用户（tests/ai 的 sys.path 里没有 content 的 helpers）。"""
    async with AsyncSessionLocal() as s:
        u = User(open_id="tags_" + uuid.uuid4().hex[:24], nickname="pytest_tags")
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


@pytest.mark.asyncio
async def test_agent_final_的标签会落库():
    """`rewrite_tags` → `distilled_articles.tags`。"""
    from tasks import distill_task

    user_id = await _new_user()
    slug = f"persist_tag_{uuid.uuid4().hex[:12]}"
    tag_name = f"科技{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as s:
        s.add(Tag(slug=slug, name=tag_name, category="test", is_system=False, creator_id=1))
        await s.commit()

    article_id = f"art_{uuid.uuid4().hex[:24]}"
    task_id = f"dst_{uuid.uuid4().hex[:24]}"
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=article_id,
                user_id=user_id,
                url="https://example.com/x",
                title="标题",
                source="wechat",
            )
        )
        await s.commit()

    await distill_task._persist_agent_final(
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        final={
            "status": "done",
            "rewritten_script": "稿子",
            "tts_audio_url": "https://example.com/a.mp3",
            "tts_duration_sec": 100,
            "rewrite_tags": [tag_name],
        },
    )

    async with AsyncSessionLocal() as s:
        da = await s.scalar(
            select(DistilledArticle).where(DistilledArticle.article_id == article_id)
        )
        assert da is not None, "蒸馏结果行没建出来"
        assert da.tags == [tag_name], f"标签没落库：{da.tags}"

    async with AsyncSessionLocal() as s:
        await s.execute(delete(DistilledArticle).where(DistilledArticle.article_id == article_id))
        await s.execute(delete(Article).where(Article.id == article_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_agent_final_不认识的标签会被过滤掉():
    """LLM 输出了表里没有的名字 → 不写进 tags（推送查不到，写了也是垃圾）。"""
    from tasks import distill_task

    user_id = await _new_user()
    slug = f"persist_tag_{uuid.uuid4().hex[:12]}"
    tag_name = f"财经{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as s:
        s.add(Tag(slug=slug, name=tag_name, category="test", is_system=False, creator_id=1))
        await s.commit()

    article_id = f"art_{uuid.uuid4().hex[:24]}"
    task_id = f"dst_{uuid.uuid4().hex[:24]}"
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=article_id,
                user_id=user_id,
                url="https://example.com/x",
                title="标题",
                source="wechat",
            )
        )
        await s.commit()

    await distill_task._persist_agent_final(
        task_id=task_id,
        article_id=article_id,
        user_id=user_id,
        final={
            "status": "done",
            "rewritten_script": "稿子",
            "rewrite_tags": ["科技资讯", "不存在的标签"],  # 都不是表里的名字
        },
    )

    async with AsyncSessionLocal() as s:
        da = await s.scalar(
            select(DistilledArticle).where(DistilledArticle.article_id == article_id)
        )
        assert da is not None
        assert not da.tags, f"不认识的标签不该落库：{da.tags}"

    async with AsyncSessionLocal() as s:
        await s.execute(delete(DistilledArticle).where(DistilledArticle.article_id == article_id))
        await s.execute(delete(Article).where(Article.id == article_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()
