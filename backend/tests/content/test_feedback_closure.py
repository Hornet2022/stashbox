"""CP5.5 文章反馈闭环（v1 §3.1 / §4.3.4）：4 端点写 feedback 表（9 case）。

覆盖：favorite / skip(+reason) / skip 缺 reason 400 / listen-complete(+listened_at)
      / rate(1-5) / rate 越界 400 / 非 owner 403 / 文章不存在 404 / 未登录 401。

前置：本机 PG 5432 + Redis 6379 已起，alembic 已到 0004（feedback 表存在）。
"""
import uuid

from sqlalchemy import delete, select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, Feedback

from helpers import client, new_article, new_user

FAVORITE_URL = "/api/v1/articles/{}/favorite"
SKIP_URL = "/api/v1/articles/{}/skip"
LISTEN_COMPLETE_URL = "/api/v1/articles/{}/listen-complete"
RATE_URL = "/api/v1/articles/{}/rate"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _feedback_rows(article_id: str) -> list[Feedback]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(Feedback).where(Feedback.article_id == article_id).order_by(Feedback.id)
        )
        return list(r.scalars().all())


async def _article(article_id: str) -> Article | None:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Article).where(Article.id == article_id))
        return r.scalar_one_or_none()


async def _purge(*user_ids: int) -> None:
    """清掉测试造的 feedback + articles（feedback 有 FK，先删 feedback）。"""
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Feedback).where(Feedback.user_id.in_(user_ids)))
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


# ---------------------------------------------------------------------------
# 1. favorite → feedback(type=favorite) + article.favorite=True
# ---------------------------------------------------------------------------
async def test_favorite_writes_feedback_and_sets_flag():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(FAVORITE_URL.format(art_id))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == art_id
        assert body["favorite"] is True
        assert isinstance(body["feedback_id"], int)

        rows = await _feedback_rows(art_id)
        assert len(rows) == 1
        fb = rows[0]
        assert fb.id == body["feedback_id"]
        assert fb.user_id == uid
        assert fb.type == "favorite"
        assert fb.rating is None and fb.reason is None
        assert fb.metadata_ == {}

        art = await _article(art_id)
        assert art.favorite is True
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 2. skip → feedback(type=skip, reason=...) + article.skip=True
# ---------------------------------------------------------------------------
async def test_skip_writes_feedback_with_reason_and_sets_flag():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(SKIP_URL.format(art_id), json={"reason": "boring"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == art_id
        assert body["skip"] is True
        assert isinstance(body["feedback_id"], int)

        rows = await _feedback_rows(art_id)
        assert len(rows) == 1
        assert rows[0].type == "skip"
        assert rows[0].reason == "boring"
        assert rows[0].user_id == uid

        art = await _article(art_id)
        assert art.skip is True
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 3. skip 缺 reason → 400（且不落 feedback / 不改 article.skip）
# ---------------------------------------------------------------------------
async def test_skip_without_reason_returns_400():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(SKIP_URL.format(art_id), json={})

        assert r.status_code == 400, r.text
        assert r.json()["message"] == "reason is required"

        # 非法枚举值也 400
        async with client(token) as c:
            r2 = await c.post(SKIP_URL.format(art_id), json={"reason": "whatever"})
        assert r2.status_code == 400, r2.text

        assert await _feedback_rows(art_id) == []
        art = await _article(art_id)
        assert art.skip is False
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 4. listen-complete → feedback(type=listen_complete) + listened_at
# ---------------------------------------------------------------------------
async def test_listen_complete_writes_feedback_and_listened_at():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(LISTEN_COMPLETE_URL.format(art_id), json={"duration_sec": 300})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == art_id
        assert body["listened_at"], "listened_at 应非空"
        assert isinstance(body["feedback_id"], int)

        rows = await _feedback_rows(art_id)
        assert len(rows) == 1
        assert rows[0].type == "listen_complete"
        assert rows[0].metadata_ == {"duration_sec": 300}
        assert rows[0].user_id == uid
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 5. rate → feedback(type=rate, rating=4, metadata={comment})
# ---------------------------------------------------------------------------
async def test_rate_writes_feedback_with_rating():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(RATE_URL.format(art_id), json={"rating": 4, "comment": "不错"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == art_id
        assert body["rating"] == 4
        assert isinstance(body["feedback_id"], int)

        rows = await _feedback_rows(art_id)
        assert len(rows) == 1
        assert rows[0].type == "rate"
        assert rows[0].rating == 4
        assert rows[0].metadata_ == {"comment": "不错"}
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 6. rate 越界（0 / 6 / 缺字段）→ 400
# ---------------------------------------------------------------------------
async def test_rate_out_of_range_returns_400():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r0 = await c.post(RATE_URL.format(art_id), json={"rating": 0})
            r6 = await c.post(RATE_URL.format(art_id), json={"rating": 6})
            r_none = await c.post(RATE_URL.format(art_id), json={})

        assert r0.status_code == 400, r0.text
        assert r6.status_code == 400, r6.text
        assert r_none.status_code == 400, r_none.text
        assert await _feedback_rows(art_id) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 7. 非 owner → 403
# ---------------------------------------------------------------------------
async def test_non_owner_returns_403():
    owner_id, _ = await new_user()
    other_id, other_token = await new_user()
    art_id = await new_article(owner_id)
    try:
        async with client(other_token) as c:
            r_fav = await c.post(FAVORITE_URL.format(art_id))
            r_skip = await c.post(SKIP_URL.format(art_id), json={"reason": "other"})
            r_lc = await c.post(LISTEN_COMPLETE_URL.format(art_id))
            r_rate = await c.post(RATE_URL.format(art_id), json={"rating": 5})

        for r in (r_fav, r_skip, r_lc, r_rate):
            assert r.status_code == 403, r.text
        assert await _feedback_rows(art_id) == []
    finally:
        await _purge(owner_id, other_id)


# ---------------------------------------------------------------------------
# 8. 文章不存在 → 404
# ---------------------------------------------------------------------------
async def test_article_not_found_returns_404():
    uid, token = await new_user()
    missing = f"art_{uuid.uuid4().hex[:24]}"
    try:
        async with client(token) as c:
            r_fav = await c.post(FAVORITE_URL.format(missing))
            r_lc = await c.post(LISTEN_COMPLETE_URL.format(missing))
            r_rate = await c.post(RATE_URL.format(missing), json={"rating": 3})

        for r in (r_fav, r_lc, r_rate):
            assert r.status_code == 404, r.text
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 9. 未登录 → 401
# ---------------------------------------------------------------------------
async def test_unauthorized_returns_401():
    uid, _ = await new_user()
    art_id = await new_article(uid)
    try:
        async with client() as c:  # 不带 Authorization
            r_fav = await c.post(FAVORITE_URL.format(art_id))
            r_skip = await c.post(SKIP_URL.format(art_id), json={"reason": "other"})
            r_lc = await c.post(LISTEN_COMPLETE_URL.format(art_id))
            r_rate = await c.post(RATE_URL.format(art_id), json={"rating": 5})

        for r in (r_fav, r_skip, r_lc, r_rate):
            assert r.status_code == 401, r.text
        assert await _feedback_rows(art_id) == []
    finally:
        await _purge(uid)
