"""CP5.5-A3 反馈分类 + 评分（feedback_v2 双轨）：11 case。

覆盖：
- 提交反馈（带 category + rating + article_id + contact + device_info）
- 提交反馈（只 category + content，无 rating / 无 article_id）
- 422：category 非法（不在枚举）
- 422：rating 越界（0 / 6）
- 422：content 为空
- 404：article_id 不存在
- 401：未登录
- 列出我的反馈
- 按 category 过滤
- 排序：最新的在前面
- CHECK constraint：rating 越界 DB 拒绝（用 SQLAlchemy 直接 insert 测试）

前置：本机 PG 5432 + Redis 6379 已起，alembic 已到 0014（feedback_v2 表存在）。
"""
import uuid

from sqlalchemy import delete, select, text

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, Feedback, FeedbackV2

from helpers import client, new_article, new_user

FEEDBACK_V2_URL = "/api/v1/feedback-v2"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _feedback_v2_rows(user_id: int, category: str | None = None) -> list[FeedbackV2]:
    async with AsyncSessionLocal() as s:
        q = select(FeedbackV2).where(FeedbackV2.user_id == user_id)
        if category:
            q = q.where(FeedbackV2.category == category)
        q = q.order_by(FeedbackV2.id)
        r = await s.execute(q)
        return list(r.scalars().all())


async def _purge(*user_ids: int) -> None:
    """清掉测试造的 feedback_v2 + feedback + articles（feedback 有 FK 依赖 articles）。"""
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(FeedbackV2).where(FeedbackV2.user_id.in_(user_ids)))
        await s.execute(delete(Feedback).where(Feedback.user_id.in_(user_ids)))
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


# ---------------------------------------------------------------------------
# 1. 提交反馈（带 category + rating + article_id + contact + device_info）
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_full():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(
                FEEDBACK_V2_URL,
                json={
                    "article_id": art_id,
                    "category": "content",
                    "rating": 5,
                    "content": "文章很棒",
                    "contact": "wechat: test123",
                    "device_info": {"app_version": "1.0.0", "os": "iOS 17"},
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["id"], int)
        assert body["category"] == "content"

        rows = await _feedback_v2_rows(uid)
        assert len(rows) == 1
        fb = rows[0]
        assert fb.article_id == art_id
        assert fb.category == "content"
        assert fb.rating == 5
        assert fb.content == "文章很棒"
        assert fb.contact == "wechat: test123"
        assert fb.device_info == {"app_version": "1.0.0", "os": "iOS 17"}
        assert fb.user_id == uid
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 2. 提交反馈（只 category + content，无 rating / 无 article_id）
# 注意：不触发 track（feedback 表 article_id 为 NOT NULL FK）
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_minimal():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            r = await c.post(
                FEEDBACK_V2_URL,
                json={"category": "bug", "content": "播放失败"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["category"] == "bug"

        rows = await _feedback_v2_rows(uid)
        assert len(rows) == 1
        fb = rows[0]
        assert fb.article_id is None
        assert fb.category == "bug"
        assert fb.rating is None
        assert fb.content == "播放失败"
        assert fb.contact is None
        assert fb.device_info is None
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 3. 422：category 非法（不在枚举）
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_invalid_category():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            r = await c.post(
                FEEDBACK_V2_URL,
                json={"category": "invalid_category", "content": "test"},
            )

        assert r.status_code == 422, r.text
        assert "category 必须是" in r.json()["detail"]
        assert await _feedback_v2_rows(uid) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 4. 422：rating 越界（0 / 6）
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_rating_out_of_range():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            r0 = await c.post(FEEDBACK_V2_URL, json={"category": "bug", "rating": 0, "content": "a"})
            r6 = await c.post(FEEDBACK_V2_URL, json={"category": "bug", "rating": 6, "content": "a"})

        assert r0.status_code == 422, r0.text
        assert "rating 必须在 1-5 之间" in r0.json()["detail"]
        assert r6.status_code == 422, r6.text
        assert await _feedback_v2_rows(uid) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 5. 422：content 为空
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_empty_content():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            r_empty = await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": ""})
            r_space = await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": "  "})

        assert r_empty.status_code == 422, r_empty.text
        assert "content 必填" in r_empty.json()["detail"]
        assert r_space.status_code == 422, r_space.text
        assert await _feedback_v2_rows(uid) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 6. 404：article_id 不存在
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_article_not_found():
    uid, token = await new_user()
    missing = f"art_{uuid.uuid4().hex[:24]}"
    try:
        async with client(token) as c:
            r = await c.post(
                FEEDBACK_V2_URL,
                json={"article_id": missing, "category": "content", "content": "test"},
            )

        assert r.status_code == 404, r.text
        assert "article 不存在" in r.json()["detail"]
        assert await _feedback_v2_rows(uid) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 7. 401：未登录
# ---------------------------------------------------------------------------
async def test_create_feedback_v2_unauthorized():
    uid, token = await new_user()
    try:
        async with client() as c:  # 不带 Authorization
            r = await c.post(
                FEEDBACK_V2_URL,
                json={"category": "bug", "content": "test"},
            )

        assert r.status_code == 401, r.text
        assert await _feedback_v2_rows(uid) == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 8. 列出我的反馈
# ---------------------------------------------------------------------------
async def test_list_my_feedback_v2():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        # 写两条
        async with client(token) as c:
            await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": "a1", "rating": 3})
            await c.post(FEEDBACK_V2_URL, json={"category": "content", "content": "a2", "article_id": art_id})

        async with client(token) as c:
            r = await c.get(FEEDBACK_V2_URL)

        assert r.status_code == 200, r.text
        body = r.json()
        assert "feedbacks" in body
        assert len(body["feedbacks"]) == 2
        # 最新在前
        assert body["feedbacks"][0]["category"] == "content"
        assert body["feedbacks"][1]["category"] == "bug"
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 9. 按 category 过滤
# ---------------------------------------------------------------------------
async def test_list_my_feedback_v2_filter_by_category():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": "b1"})
            await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": "b2"})
            await c.post(FEEDBACK_V2_URL, json={"category": "content", "content": "c1"})

        async with client(token) as c:
            r = await c.get(FEEDBACK_V2_URL, params={"category": "bug"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["feedbacks"]) == 2
        for fb in body["feedbacks"]:
            assert fb["category"] == "bug"

        async with client(token) as c:
            r2 = await c.get(FEEDBACK_V2_URL, params={"category": "feature"})
        assert r2.status_code == 200, r2.text
        assert len(r2.json()["feedbacks"]) == 0
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 10. 排序：最新的在前面
# ---------------------------------------------------------------------------
async def test_list_my_feedback_v2_order_by_created_at_desc():
    uid, token = await new_user()
    try:
        async with client(token) as c:
            await c.post(FEEDBACK_V2_URL, json={"category": "other", "content": "first"})
            await c.post(FEEDBACK_V2_URL, json={"category": "bug", "content": "second"})
            await c.post(FEEDBACK_V2_URL, json={"category": "content", "content": "third"})

        async with client(token) as c:
            r = await c.get(FEEDBACK_V2_URL)

        assert r.status_code == 200, r.text
        body = r.json()
        cats = [fb["category"] for fb in body["feedbacks"]]
        assert cats == ["content", "bug", "other"]
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 11. CHECK constraint：rating 越界 DB 拒绝（SQLAlchemy 直接 insert）
# ---------------------------------------------------------------------------
async def test_feedback_v2_check_constraint_rating_range():
    uid, token = await new_user()
    try:
        async with AsyncSessionLocal() as s:
            # rating = 0 违反 ck_feedback_v2_rating_range
            fb = FeedbackV2(
                user_id=uid,
                article_id=None,
                category="bug",
                rating=0,
                content="test",
            )
            s.add(fb)
            try:
                await s.commit()
            except Exception:
                await s.rollback()
                # 预期 DB 拒绝
            else:
                raise AssertionError("rating=0 should have been rejected by DB CHECK constraint")

        async with AsyncSessionLocal() as s:
            # rating = 6 违反 ck_feedback_v2_rating_range
            fb2 = FeedbackV2(
                user_id=uid,
                article_id=None,
                category="bug",
                rating=6,
                content="test",
            )
            s.add(fb2)
            try:
                await s.commit()
            except Exception:
                await s.rollback()
            else:
                raise AssertionError("rating=6 should have been rejected by DB CHECK constraint")
    finally:
        await _purge(uid)
