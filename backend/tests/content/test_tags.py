"""tags 表 + GET /api/v1/tags DB 化（CP5.3a）。v1 §11.5 CP5.3 后端基础。

前置条件：alembic upgrade 0007 已跑（dev DB 升级由 Hornet 手动执行）。
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from helpers import content_main, new_user
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, Tag, TagSubscription


def _override_auth(dep_name: str, mock_user: dict):
    """临时覆盖 content-service 的鉴权依赖（dependency_overrides）。"""

    dep = getattr(content_main, dep_name)

    class _Ctx:
        def __enter__(self):
            content_main.app.dependency_overrides[dep] = lambda: mock_user
            return self

        def __exit__(self, *exc):
            content_main.app.dependency_overrides.pop(dep, None)

    return _Ctx()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _unique_slug():
    return f"test_tag_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# CP5.3a existing tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tags_table_exists():
    """alembic 0007 后 tags 表存在 + 4 个 seed（tech/finance/life/news）"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT slug, name, category FROM tags ORDER BY slug"))
        rows = r.fetchall()
        assert len(rows) >= 4, f"tags 表只有 {len(rows)} 行（期望 ≥ 4 seed）"


@pytest.mark.asyncio
async def test_tag_subscriptions_table_exists():
    """tag_subscriptions 表存在"""
    async with AsyncSessionLocal() as s:
        await s.execute(text("SELECT 1 FROM tag_subscriptions LIMIT 1"))
        # 不管返不返 rows，只确认表存在
        # 如果表不存在 SQLAlchemy 抛 ProgrammingError


# ---------------------------------------------------------------------------
# CP5.3b new tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_create_tag():
    """admin create 端点：mock user 创 tag（幂等 slug → 409）

    ⚠️ 2026-10-03 修：原来 name 写死 `"测试标签"`，而 cleanup 只按 slug 删。
    slug 每次随机所以**没被清掉的行会一直累积**（stashbox_test 里已堆到 33 行），
    而 name 固定 → 第二次跑必然 `409 tag name 已存在`。
    也就是说这条用例**只能跑一次**，第二遍必红 —— 很容易被误判成回归。

    现在 name 也带唯一后缀，两边都能对上。
    """
    slug = _unique_slug()
    name = f"测试标签-{slug[-8:]}"
    # 原来这里写死 {"id": 999, ...}，但 tags.creator_id 有外键指向 users.id，
    # users 表里并没有 999 → ForeignKeyViolation（test_admin_create_tag 长期红）。
    admin_id, _ = await new_user(tier="admin")
    mock_user = {"id": admin_id, "sub": str(admin_id), "tier": "admin"}

    with _override_auth("require_admin_or_operator", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tags",
                json={"slug": slug, "name": name, "category": "test"},
            )
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["id"] == slug
        assert data["name"] == name
        assert data["category"] == "test"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_admin_create_tag_duplicate_409():
    """重复 slug → 409"""
    slug = _unique_slug()
    # 原来这里写死 {"id": 999, ...}，但 tags.creator_id 有外键指向 users.id，
    # users 表里并没有 999 → ForeignKeyViolation（test_admin_create_tag 长期红）。
    admin_id, _ = await new_user(tier="admin")
    mock_user = {"id": admin_id, "sub": str(admin_id), "tier": "admin"}

    # 创建第一个
    with _override_auth("require_admin_or_operator", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/tags", json={"slug": slug, "name": "标签A"})
            # 重复创建
            resp = await client.post("/api/v1/tags", json={"slug": slug, "name": "标签B"})

        assert resp.status_code == 409, f"期望 409，实际 {resp.status_code}: {resp.text}"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_user_subscribe_tag_idempotent():
    """subscribe 端点：幂等（重复订阅返 already_subscribed）+ 写 tag_subscriptions"""
    slug = _unique_slug()
    user_id, _tok = await new_user()

    # 先建一个 tag
    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name="sub_test", category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()
        tag_id = t.id

    mock_user = {"id": user_id, "sub": str(user_id)}

    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 第一次订阅
            resp1 = await client.post(f"/api/v1/tags/{slug}/subscribe")
            assert resp1.status_code == 200, f"第一次订阅失败: {resp1.text}"
            data1 = resp1.json()
            assert data1["ok"] is True
            assert "already_subscribed" not in data1

            # 第二次订阅（幂等）
            resp2 = await client.post(f"/api/v1/tags/{slug}/subscribe")
            assert resp2.status_code == 200, f"幂等订阅失败: {resp2.text}"
            data2 = resp2.json()
            assert data2["ok"] is True
            assert data2.get("already_subscribed") is True

            # 用数字 id 也行
            resp3 = await client.post(f"/api/v1/tags/{tag_id}/subscribe")
            assert resp3.status_code == 200
            assert resp3.json().get("already_subscribed") is True

    # verify DB
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(TagSubscription).where(
                TagSubscription.user_id == user_id,
                TagSubscription.tag_id == tag_id,
            )
        )
        sub = r.scalar_one_or_none()
        assert sub is not None, "tag_subscriptions 应该有一行"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_user_unsubscribe_tag_idempotent():
    """unsubscribe 端点：幂等（本来没订阅也返 already_unsubscribed）+ 删 tag_subscriptions"""
    slug = _unique_slug()
    user_id, _tok = await new_user()

    # 先建一个 tag
    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name="unsub_test", category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()
        tag_id = t.id

    mock_user = {"id": user_id, "sub": str(user_id)}

    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 本来就没订阅 → already_unsubscribed
            resp1 = await client.post(f"/api/v1/tags/{slug}/unsubscribe")
            assert resp1.status_code == 200, f"未订阅取消失败: {resp1.text}"
            data1 = resp1.json()
            assert data1["ok"] is True
            assert data1.get("already_unsubscribed") is True

            # 订阅后再取消
            await client.post(f"/api/v1/tags/{slug}/subscribe")
            resp2 = await client.post(f"/api/v1/tags/{slug}/unsubscribe")
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["ok"] is True
            assert "already_unsubscribed" not in data2

            # 数字 id 也可
            resp3 = await client.post(f"/api/v1/tags/{tag_id}/unsubscribe")
            assert resp3.status_code == 200
            assert resp3.json().get("already_unsubscribed") is True

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


# ---------------------------------------------------------------------------
# 2026-10-26 产品拍板：list_tags 同时返回 article_count（自己蒸馏）与
# subscribable_count（订阅可得，全站 da.tags ∪ shared_tags 已就绪）。
# 语义与订阅 feed 的 total 完全一致 —— 两个数字本就不该相等。
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_tags_subscribable_count():
    """GET /api/v1/tags：article_count 只算自己蒸馏；subscribable_count 算全站订阅可得。

    口径（与实现/文档一致）：
      · article_count = 该用户**自己**蒸馏且 da.tags 命中该标签的文章数 —— **不看就绪状态**
        （文档「已蒸馏」，running 也算；它只是「我蒸馏了多少」，不承诺可听）。
      · subscribable_count = **全站** da.tags ∪ shared_tags 命中、且**已就绪**（done+有音频）的文章数。

    场景：
      · 自己蒸馏 done（da.tags 命中）→ article_count + subscribable_count 都算；
      · 自己蒸馏 running（da.tags 命中）→ 只算 article_count，不进 subscribable_count；
      · 别人蒸馏 done（da.tags 命中）→ 只进 subscribable_count；
      · 别人共享（da.tags 空、shared_tags 命中）→ 只进 subscribable_count。
    期望：article_count=2（自己 done+running），subscribable_count=3（自己 done + 别人 done + 别人共享）。
    """
    slug = _unique_slug()
    tag_name = f"科技-{slug[-6:]}"
    user_id, _tok = await new_user()
    other_id, _ = await new_user()

    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name=tag_name, category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()

    async with AsyncSessionLocal() as s:
        # 自己蒸馏、已就绪 → article_count + subscribable_count
        a1 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url="https://e/1",
            title="自己",
            source="wechat",
        )
        s.add(a1)
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=a1.id,
                status="done",
                audio_url="https://x/a.mp3",
                tags=[tag_name],
            )
        )
        # 别人蒸馏、已就绪 → 只进 subscribable_count
        a2 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=other_id,
            url="https://e/2",
            title="别人",
            source="wechat",
        )
        s.add(a2)
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=a2.id,
                status="done",
                audio_url="https://x/b.mp3",
                tags=[tag_name],
            )
        )
        # 别人共享（da.tags 空、shared_tags 命中）→ 只进 subscribable_count
        a3 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=other_id,
            url="https://e/3",
            title="共享",
            source="wechat",
            shared_tags=[tag_name],
        )
        s.add(a3)
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=a3.id,
                status="done",
                audio_url="https://x/c.mp3",
                tags=[],
            )
        )
        # 自己未就绪 → 只算 article_count（自己蒸馏过），不进 subscribable_count
        a4 = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url="https://e/4",
            title="未就绪",
            source="wechat",
        )
        s.add(a4)
        s.add(
            DistilledArticle(
                id=f"dst_{uuid.uuid4().hex[:24]}",
                article_id=a4.id,
                status="running",
                tags=[tag_name],
            )
        )
        await s.commit()

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/tags")
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text}"

    target = next(t for t in resp.json()["tags"] if t["id"] == slug)
    assert (
        target["article_count"] == 2
    ), f"article_count 应为 2（自己蒸馏 done+running，不看就绪），实际 {target['article_count']}"
    assert (
        target["subscribable_count"] == 3
    ), f"subscribable_count 应为 3（全站订阅可得已就绪），实际 {target['subscribable_count']}"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(
            delete(DistilledArticle).where(
                DistilledArticle.article_id.in_([a1.id, a2.id, a3.id, a4.id])
            )
        )
        await s.execute(delete(Article).where(Article.id.in_([a1.id, a2.id, a3.id, a4.id])))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()
