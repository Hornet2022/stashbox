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
from stashbox.backend.common.models.push_notification import PushNotification


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
# 2026-10-27：退订必须连带删掉该用户该标签的**已推送通知**
#
# ## 为什么要这条测试
#
# 用户点通知进详情页 → 客户端报「请求失败（403）」。根因是**两边口径不一致**：
#   · 写侧：退订只删 tag_subscriptions，不动已推的 push_notifications；
#   · 读侧：_subscription_grants_access 只认**当前**订阅，非属主拿不到文章。
# ⇒ 退订后，历史通知全成死链。
#
# 这类缺陷**不会自己暴露**：接口都返回 200，数据也自洽，
# 只有用户真的点进去才发现。⇒ 钉成测试。
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unsubscribe_deletes_own_tag_notifications():
    """退订该标签 ⇒ 该用户该标签的通知被清掉；别人的通知不受影响。"""
    slug = _unique_slug()
    user_id, _tok = await new_user()
    other_id, _tok2 = await new_user()

    async with AsyncSessionLocal() as s:
        t = Tag(slug=slug, name="unsub_notif_test", category="test", is_system=False, creator_id=1)
        s.add(t)
        await s.commit()

    # 造两条通知：一条属于本用户、一条属于另一个用户，都指向同一个 tag
    async with AsyncSessionLocal() as s:
        s.add_all(
            [
                PushNotification(
                    user_id=user_id,
                    article_id=None,
                    tag_slug=slug,
                    title="给本用户的通知",
                    body="body",
                    status="sent",
                ),
                PushNotification(
                    user_id=other_id,
                    article_id=None,
                    tag_slug=slug,
                    title="给别人的通知",
                    body="body",
                    status="sent",
                ),
            ]
        )
        await s.commit()

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(f"/api/v1/tags/{slug}/subscribe")
            resp = await client.post(f"/api/v1/tags/{slug}/unsubscribe")
            assert resp.status_code == 200, f"退订失败: {resp.text}"
            data = resp.json()
            assert data["ok"] is True
            assert (
                data.get("deleted_notifications") == 1
            ), f"退订应删掉本用户该标签的 1 条通知，实际 {data.get('deleted_notifications')}"

    async with AsyncSessionLocal() as s:
        mine = (
            (
                await s.execute(
                    select(PushNotification).where(
                        PushNotification.user_id == user_id,
                        PushNotification.tag_slug == slug,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(mine) == 0, "退订后本用户该标签的通知必须被清掉（否则点进去必然 403）"

        others = (
            (
                await s.execute(
                    select(PushNotification).where(
                        PushNotification.user_id == other_id,
                        PushNotification.tag_slug == slug,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(others) == 1, "别人的通知不能被误删"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(PushNotification).where(
                PushNotification.user_id.in_([user_id, other_id]),
                PushNotification.tag_slug == slug,
            )
        )
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug == slug))
        await s.commit()


@pytest.mark.asyncio
async def test_unsubscribe_keeps_notifications_of_other_tags():
    """退订 A 标签不能误删该用户 B 标签的通知。"""
    slug_a = _unique_slug()
    slug_b = _unique_slug()
    user_id, _tok = await new_user()

    async with AsyncSessionLocal() as s:
        s.add_all(
            [
                Tag(slug=slug_a, name="unsub_a", category="test", is_system=False, creator_id=1),
                Tag(slug=slug_b, name="unsub_b", category="test", is_system=False, creator_id=1),
            ]
        )
        await s.commit()

    async with AsyncSessionLocal() as s:
        s.add_all(
            [
                PushNotification(
                    user_id=user_id,
                    article_id=None,
                    tag_slug=slug_a,
                    title="A",
                    body="b",
                    status="sent",
                ),
                PushNotification(
                    user_id=user_id,
                    article_id=None,
                    tag_slug=slug_b,
                    title="B",
                    body="b",
                    status="sent",
                ),
            ]
        )
        await s.commit()

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(f"/api/v1/tags/{slug_a}/subscribe")
            await client.post(f"/api/v1/tags/{slug_b}/subscribe")
            resp = await client.post(f"/api/v1/tags/{slug_a}/unsubscribe")
            assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as s:
        remain = (
            (
                await s.execute(
                    select(PushNotification).where(
                        PushNotification.user_id == user_id,
                        PushNotification.tag_slug == slug_b,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remain) == 1, "退订 A 不能连带删掉 B 标签的通知"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(PushNotification).where(
                PushNotification.user_id == user_id,
                PushNotification.tag_slug.in_([slug_a, slug_b]),
            )
        )
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(Tag).where(Tag.slug.in_([slug_a, slug_b])))
        await s.commit()


# ---------------------------------------------------------------------------
# 2026-10-27 加固：判据是「文章还能不能读」，不是「通知挂在哪个标签下」
#
# 生产库实测（user 23 只订阅「生活」）：同时含「生活+科技/财经」的 3 篇文章里，
# 有 **2 篇**推给她的通知 tag_slug 是 tech/finance（主标签 ≠ 用户订阅的标签）
# ⇒ 按 tag_slug=life 删会漏掉这 2 条 ⇒ 死链还在。
# ---------------------------------------------------------------------------
async def _make_shared_article(owner_id: int, tag_names: list[str]):
    """造一篇「别人家的」文章 + 蒸馏产物（tags 是**标签名**）。返回 article_id。"""
    art_id = f"art_{uuid.uuid4().hex[:24]}"
    async with AsyncSessionLocal() as s:
        s.add(
            Article(
                id=art_id,
                user_id=owner_id,
                url=f"https://example.com/{art_id}",
                title="共享标签文章",
                source="web",
                status="ready",
                raw_content={"content_text": "正文" * 50, "source": "web"},
                favorite=False,
                skip=False,
            )
        )
        s.add(
            DistilledArticle(
                id=f"dst_{art_id[4:]}",
                article_id=art_id,
                status="done",
                script_text="稿子" * 40,
                audio_url="https://oss.example.com/a.mp3",
                duration_sec=60,
                tags=tag_names,
            )
        )
        await s.commit()
    return art_id


@pytest.mark.asyncio
async def test_unsubscribe_deletes_notification_whose_tag_is_not_the_unsubscribed_one():
    """退订 A ⇒ 通知挂在 B 标签下、但文章已经读不了了 ⇒ 也必须删掉。

    场景 = 生产实测：通知的 tag_slug 是**文章主标签**（`_trigger_subscription_pushes`
    里 `primary_tag_slug = tag_slugs[0]`，取标签 id 最小的那个），
    未必是用户订阅的那个标签。
    """
    slug_a = _unique_slug()
    slug_b = _unique_slug()
    name_a, name_b = f"名A{uuid.uuid4().hex[:6]}", f"名B{uuid.uuid4().hex[:6]}"
    user_id, _tok = await new_user()
    owner_id, _tok2 = await new_user()

    async with AsyncSessionLocal() as s:
        s.add_all(
            [
                Tag(slug=slug_a, name=name_a, category="test", is_system=False, creator_id=1),
                Tag(slug=slug_b, name=name_b, category="test", is_system=False, creator_id=1),
            ]
        )
        await s.commit()

    # 文章同时属于 A、B 两个标签 ⇒ 用户只要还订阅其中任一个就还读得到
    art_id = await _make_shared_article(owner_id, [name_a, name_b])

    async with AsyncSessionLocal() as s:
        s.add(
            PushNotification(
                user_id=user_id,
                article_id=art_id,
                tag_slug=slug_a,  # ← 主标签 A，用户其实订阅的是 B
                title="通知",
                body="b",
                status="sent",
            )
        )
        await s.commit()

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(f"/api/v1/tags/{slug_b}/subscribe")
            resp = await client.post(f"/api/v1/tags/{slug_b}/unsubscribe")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data.get("deleted_notifications") == 1, (
                "退订后文章已读不了（通知 tag_slug 与被退订标签不同也要删），"
                f"实际 {data.get('deleted_notifications')}"
            )

    async with AsyncSessionLocal() as s:
        remain = (
            (
                await s.execute(
                    select(PushNotification).where(
                        PushNotification.user_id == user_id,
                        PushNotification.article_id == art_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remain) == 0, "文章已经读不了 ⇒ 这条通知是死链 ⇒ 必须被删掉"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(PushNotification).where(PushNotification.user_id == user_id))
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(DistilledArticle).where(DistilledArticle.article_id == art_id))
        await s.execute(delete(Article).where(Article.id == art_id))
        await s.execute(delete(Tag).where(Tag.slug.in_([slug_a, slug_b])))
        await s.commit()


@pytest.mark.asyncio
async def test_unsubscribe_keeps_notification_whose_article_is_still_readable():
    """反向护栏：还有别的订阅撑着文章 ⇒ 通知**必须留着**（别过度删除）。"""
    slug_a = _unique_slug()
    slug_b = _unique_slug()
    name_a, name_b = f"名A{uuid.uuid4().hex[:6]}", f"名B{uuid.uuid4().hex[:6]}"
    user_id, _tok = await new_user()
    owner_id, _tok2 = await new_user()

    async with AsyncSessionLocal() as s:
        s.add_all(
            [
                Tag(slug=slug_a, name=name_a, category="test", is_system=False, creator_id=1),
                Tag(slug=slug_b, name=name_b, category="test", is_system=False, creator_id=1),
            ]
        )
        await s.commit()

    art_id = await _make_shared_article(owner_id, [name_a, name_b])

    async with AsyncSessionLocal() as s:
        s.add(
            PushNotification(
                user_id=user_id,
                article_id=art_id,
                tag_slug=slug_a,
                title="通知",
                body="b",
                status="sent",
            )
        )
        await s.commit()

    mock_user = {"id": user_id, "sub": str(user_id)}
    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(f"/api/v1/tags/{slug_a}/subscribe")
            await client.post(f"/api/v1/tags/{slug_b}/subscribe")
            resp = await client.post(f"/api/v1/tags/{slug_b}/unsubscribe")
            assert resp.status_code == 200, resp.text
            assert (
                resp.json().get("deleted_notifications") == 0
            ), "还订阅着 A ⇒ 文章仍读得到 ⇒ 不该删这条通知"

    async with AsyncSessionLocal() as s:
        remain = (
            (
                await s.execute(
                    select(PushNotification).where(
                        PushNotification.user_id == user_id,
                        PushNotification.article_id == art_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remain) == 1, "文章还能读 ⇒ 通知必须留着"

    # cleanup
    async with AsyncSessionLocal() as s:
        await s.execute(delete(PushNotification).where(PushNotification.user_id == user_id))
        await s.execute(delete(TagSubscription).where(TagSubscription.user_id == user_id))
        await s.execute(delete(DistilledArticle).where(DistilledArticle.article_id == art_id))
        await s.execute(delete(Article).where(Article.id == art_id))
        await s.execute(delete(Tag).where(Tag.slug.in_([slug_a, slug_b])))
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
