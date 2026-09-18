"""CP5.5 稍后听（later_listens）：4+ case。

覆盖：
- ✅ snooze（带 snooze_until）
- ✅ snooze（不带 snooze_until）
- ✅ 重复 snooze → update
- ✅ 列稍后听
- ✅ 取消稍后听
- ✅ 取消不存在的稍后听 → was_snoozed=false
"""
from sqlalchemy import delete, select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, LaterListen

from helpers import client, new_article, new_user

SNOOZE_URL = "/api/v1/articles/{}/snooze"
LIST_URL = "/api/v1/later-listens"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _snooze_rows(user_id: int) -> list[LaterListen]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(LaterListen).where(LaterListen.user_id == user_id).order_by(LaterListen.id)
        )
        return list(r.scalars().all())


async def _purge(*user_ids: int) -> None:
    """清掉测试造的 later_listens + articles。"""
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(LaterListen).where(LaterListen.user_id.in_(user_ids)))
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


# ---------------------------------------------------------------------------
# 1. snooze（带 snooze_until）
# ---------------------------------------------------------------------------
async def test_snooze_with_until():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(
                SNOOZE_URL.format(art_id),
                json={"snooze_until": "2026-09-19T20:00:00Z"}
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["id"], int)

        rows = await _snooze_rows(uid)
        assert len(rows) == 1
        assert rows[0].article_id == art_id
        assert rows[0].snooze_until is not None
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 2. snooze（不带 snooze_until）
# ---------------------------------------------------------------------------
async def test_snooze_without_until():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(SNOOZE_URL.format(art_id), json={})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True

        rows = await _snooze_rows(uid)
        assert len(rows) == 1
        assert rows[0].snooze_until is None
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 3. 重复 snooze → update
# ---------------------------------------------------------------------------
async def test_resnooze_updates_existing():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r1 = await c.post(SNOOZE_URL.format(art_id), json={"snooze_until": "2026-09-19T20:00:00Z"})
            r2 = await c.post(SNOOZE_URL.format(art_id), json={"snooze_until": "2026-09-20T20:00:00Z"})

        assert r1.status_code == 200
        assert r2.json()["updated"] is True
        assert r2.json()["id"] == r1.json()["id"]

        rows = await _snooze_rows(uid)
        assert len(rows) == 1
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 4. 列稍后听
# ---------------------------------------------------------------------------
async def test_list_later_listens():
    uid, token = await new_user()
    art1 = await new_article(uid)
    art2 = await new_article(uid)
    try:
        async with client(token) as c:
            await c.post(SNOOZE_URL.format(art1), json={"snooze_until": "2026-09-19T20:00:00Z"})
            await c.post(SNOOZE_URL.format(art2), json={})

            r = await c.get(LIST_URL)

        assert r.status_code == 200
        items = r.json()["later_listens"]
        assert len(items) == 2
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 5. 取消稍后听
# ---------------------------------------------------------------------------
async def test_unsnooze():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            await c.post(SNOOZE_URL.format(art_id), json={"snooze_until": "2026-09-19T20:00:00Z"})

            del_r = await c.delete(SNOOZE_URL.format(art_id))
            list_r = await c.get(LIST_URL)

        assert del_r.status_code == 200
        assert list_r.json()["later_listens"] == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 6. 取消不存在的稍后听 → was_snoozed=false
# ---------------------------------------------------------------------------
async def test_unsnooze_nonexistent_returns_was_snoozed_false():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.delete(SNOOZE_URL.format(art_id))

        assert r.status_code == 200
        assert r.json()["was_snoozed"] is False
    finally:
        await _purge(uid)
