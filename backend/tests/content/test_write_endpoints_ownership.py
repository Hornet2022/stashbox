"""收藏 / 稍后听 / 收听进度 —— 属主校验（2026-10-22 走查补齐）。

## 为什么补这三条

这三个写端点原来只查「文章**存在**」，不查「文章属于当前用户」。
同仓库的兄弟端点（mark-listened、detail、delete…）都用
`_get_owned(article_id, uid, db)`，判据是「属主不符 → 403」。

口径不一致的后果不是越权读（别人的文章 id 是 art_ + uuid4 前 24 位，
不可猜），而是**数据完整性**：
写进去的行挂在**调用者**名下 → 调用者的收藏列表里会出现
**打不开（详情接口 403）**的条目。写的时候说"存在"，
读的时候被属主校验拦掉 —— 用户看到的就是一个坏掉的收藏。

`update_progress` 的注释还写着「检查 article 是否存在（归属校验）」——
**注释声称的归属校验并不存在**。这类"注释说有、代码没有"最坏：
读代码的人会以为已经覆盖了。

## 本文件锁住

A. 收藏：别人的文章 → 403（不写库）
B. 稍后听：别人的文章 → 403（不写库）
C. 进度：别人的文章 → 403（不写库）
D. 自己的文章：三个端点都正常（别把正常路径也堵死）
"""

from sqlalchemy import delete, select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, Favorite, LaterListen, ListeningStatus

from helpers import client, new_article, new_user

FAV_URL = "/api/v1/articles/{}/favorites"
SNOOZE_URL = "/api/v1/articles/{}/snooze"
PROGRESS_URL = "/api/v1/articles/{}/progress"


async def _purge(*user_ids: int) -> None:
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Favorite).where(Favorite.user_id.in_(user_ids)))
        await s.execute(delete(LaterListen).where(LaterListen.user_id.in_(user_ids)))
        await s.execute(delete(ListeningStatus).where(ListeningStatus.user_id.in_(user_ids)))
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


async def test_add_favorite_on_other_users_article_forbidden():
    """A. 给别人的文章加收藏 → 403，且不写库。"""
    owner_id, _ = await new_user()
    caller_id, caller_token = await new_user()
    article_id = await new_article(owner_id)
    try:
        async with client(caller_token) as c:
            r = await c.post(FAV_URL.format(article_id), json={"folder": "default"})
        assert r.status_code == 403, f"给别人文章加收藏没被拦住：{r.text}"
        async with AsyncSessionLocal() as s:
            rows = list(
                (
                    await s.execute(
                        select(Favorite).where(
                            Favorite.user_id == caller_id,
                            Favorite.article_id == article_id,
                        )
                    )
                ).scalars()
            )
            assert rows == [], f"403 之后还写了库：{rows}"
    finally:
        await _purge(owner_id, caller_id)


async def test_snooze_on_other_users_article_forbidden():
    """B. 把别人的文章加进「稍后听」 → 403，且不写库。"""
    owner_id, _ = await new_user()
    caller_id, caller_token = await new_user()
    article_id = await new_article(owner_id)
    try:
        async with client(caller_token) as c:
            r = await c.post(SNOOZE_URL.format(article_id), json={})
        assert r.status_code == 403, f"把别人文章加进稍后听没被拦住：{r.text}"
        async with AsyncSessionLocal() as s:
            rows = list(
                (
                    await s.execute(
                        select(LaterListen).where(
                            LaterListen.user_id == caller_id,
                            LaterListen.article_id == article_id,
                        )
                    )
                ).scalars()
            )
            assert rows == [], f"403 之后还写了库：{rows}"
    finally:
        await _purge(owner_id, caller_id)


async def test_progress_on_other_users_article_forbidden():
    """C. 给别人的文章上报收听进度 → 403，且不写库。

    注释原来写着「（归属校验）」，实际没有 —— 这条把它变成真的。
    """
    owner_id, _ = await new_user()
    caller_id, caller_token = await new_user()
    article_id = await new_article(owner_id)
    try:
        async with client(caller_token) as c:
            r = await c.post(
                PROGRESS_URL.format(article_id),
                json={"position_sec": 42, "total_sec": 100},
            )
        assert r.status_code == 403, f"给别人文章上报进度没被拦住：{r.text}"
        async with AsyncSessionLocal() as s:
            rows = list(
                (
                    await s.execute(
                        select(ListeningStatus).where(
                            ListeningStatus.user_id == caller_id,
                            ListeningStatus.article_id == article_id,
                        )
                    )
                ).scalars()
            )
            assert rows == [], f"403 之后还写了库：{rows}"
    finally:
        await _purge(owner_id, caller_id)


async def test_own_article_still_works_for_all_three():
    """D. 自己的文章，三个端点都正常 —— 别把正常路径也堵死。"""
    uid, token = await new_user()
    article_id = await new_article(uid)
    try:
        async with client(token) as c:
            r1 = await c.post(FAV_URL.format(article_id), json={"folder": "default"})
            r2 = await c.post(SNOOZE_URL.format(article_id), json={})
            r3 = await c.post(
                PROGRESS_URL.format(article_id),
                json={"position_sec": 42, "total_sec": 100},
            )
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r3.status_code == 200, r3.text
    finally:
        await _purge(uid)
