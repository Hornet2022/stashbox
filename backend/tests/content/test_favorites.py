"""CP5.5 收藏夹（folder+note 双轨）：9 case。

覆盖：
- ✅ 加收藏（带 folder + note）
- ✅ 加收藏（不带 folder = default）
- ✅ 同一文章同一 folder 重复加 → already_favorited
- ✅ 同一文章不同 folder → 双条记录
- ✅ 列收藏（按 folder 过滤）
- ✅ 列 folders（去重 + 计数）
- ✅ 改 favorite（folder + note）
- ✅ 删 favorite
- ✅ 404：文章不存在
- ✅ 401：未登录
- ✅ 403：改别人的 favorite（user_id != owner）
"""
from sqlalchemy import delete, select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, Favorite

from helpers import client, new_article, new_user

ADD_URL = "/api/v1/articles/{}/favorites"
LIST_URL = "/api/v1/favorites"
FOLDERS_URL = "/api/v1/favorites/folders"
PATCH_URL = "/api/v1/favorites/{}"
DELETE_URL = "/api/v1/favorites/{}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _fav_rows(user_id: int) -> list[Favorite]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(Favorite).where(Favorite.user_id == user_id).order_by(Favorite.id)
        )
        return list(r.scalars().all())


async def _purge(*user_ids: int) -> None:
    """清掉测试造的 favorites + articles。"""
    if not user_ids:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Favorite).where(Favorite.user_id.in_(user_ids)))
        await s.execute(delete(Article).where(Article.user_id.in_(user_ids)))
        await s.commit()


# ---------------------------------------------------------------------------
# 1. 加收藏（带 folder + note）
# ---------------------------------------------------------------------------
async def test_add_favorite_with_folder_and_note():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(ADD_URL.format(art_id), json={"folder": "tech", "note": "很棒的文章"})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["folder"] == "tech"
        assert isinstance(body["id"], int)

        rows = await _fav_rows(uid)
        assert len(rows) == 1
        assert rows[0].folder == "tech"
        assert rows[0].note == "很棒的文章"
        assert rows[0].article_id == art_id
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 2. 加收藏（不带 folder = default）
# ---------------------------------------------------------------------------
async def test_add_favorite_default_folder():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r = await c.post(ADD_URL.format(art_id), json={})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["folder"] == "default"

        rows = await _fav_rows(uid)
        assert len(rows) == 1
        assert rows[0].folder == "default"
        assert rows[0].note is None
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 3. 同一文章同一 folder 重复加 → already_favorited
# ---------------------------------------------------------------------------
async def test_add_favorite_same_folder_returns_already():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r1 = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})
            r2 = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["already_favorited"] is True
        assert r2.json()["id"] == r1.json()["id"]

        rows = await _fav_rows(uid)
        assert len(rows) == 1
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 4. 同一文章不同 folder → 双条记录
# ---------------------------------------------------------------------------
async def test_add_favorite_different_folder_creates_new_row():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            r1 = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})
            r2 = await c.post(ADD_URL.format(art_id), json={"folder": "gold"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["id"] != r2.json()["id"]

        rows = await _fav_rows(uid)
        assert len(rows) == 2
        folders = {row.folder for row in rows}
        assert folders == {"tech", "gold"}
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 5. 列收藏（按 folder 过滤）
# ---------------------------------------------------------------------------
async def test_list_favorites_filter_by_folder():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            await c.post(ADD_URL.format(art_id), json={"folder": "tech", "note": "t"})
            await c.post(ADD_URL.format(art_id), json={"folder": "gold", "note": "g"})

            r_all = await c.get(LIST_URL)
            r_tech = await c.get(LIST_URL, params={"folder": "tech"})
            r_gold = await c.get(LIST_URL, params={"folder": "gold"})

        assert r_all.status_code == 200
        assert len(r_all.json()["favorites"]) == 2

        assert r_tech.status_code == 200
        assert len(r_tech.json()["favorites"]) == 1
        assert r_tech.json()["favorites"][0]["folder"] == "tech"

        assert r_gold.status_code == 200
        assert len(r_gold.json()["favorites"]) == 1
        assert r_gold.json()["favorites"][0]["folder"] == "gold"
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 6. 列 folders（去重 + 计数）
# ---------------------------------------------------------------------------
async def test_list_favorite_folders_with_count():
    uid, token = await new_user()
    art1 = await new_article(uid)
    art2 = await new_article(uid)
    try:
        async with client(token) as c:
            await c.post(ADD_URL.format(art1), json={"folder": "tech"})
            await c.post(ADD_URL.format(art2), json={"folder": "tech"})
            await c.post(ADD_URL.format(art2), json={"folder": "gold"})

            r = await c.get(FOLDERS_URL)

        assert r.status_code == 200
        folders = {f["folder"]: f["count"] for f in r.json()["folders"]}
        assert folders["tech"] == 2
        assert folders["gold"] == 1
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 7. 改 favorite（folder + note）
# ---------------------------------------------------------------------------
async def test_update_favorite_folder_and_note():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            add_r = await c.post(ADD_URL.format(art_id), json={"folder": "tech", "note": "old"})
            fav_id = add_r.json()["id"]

            r = await c.patch(PATCH_URL.format(fav_id), json={"folder": "gold", "note": "new note"})

        assert r.status_code == 200
        rows = await _fav_rows(uid)
        assert len(rows) == 1
        assert rows[0].folder == "gold"
        assert rows[0].note == "new note"
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 8. 删 favorite
# ---------------------------------------------------------------------------
async def test_delete_favorite():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client(token) as c:
            add_r = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})
            fav_id = add_r.json()["id"]

            del_r = await c.delete(DELETE_URL.format(fav_id))
            list_r = await c.get(LIST_URL)

        assert del_r.status_code == 200
        assert list_r.json()["favorites"] == []
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 9. 404：文章不存在
# ---------------------------------------------------------------------------
async def test_add_favorite_article_not_found():
    uid, token = await new_user()
    import uuid
    missing = f"art_{uuid.uuid4().hex[:24]}"
    try:
        async with client(token) as c:
            r = await c.post(ADD_URL.format(missing), json={"folder": "tech"})

        assert r.status_code == 404, r.text
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 10. 401：未登录
# ---------------------------------------------------------------------------
async def test_add_favorite_unauthorized():
    uid, token = await new_user()
    art_id = await new_article(uid)
    try:
        async with client() as c:
            r = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})

        assert r.status_code == 401, r.text
    finally:
        await _purge(uid)


# ---------------------------------------------------------------------------
# 11. 403：改别人的 favorite
# ---------------------------------------------------------------------------
async def test_update_other_user_favorite_returns_404():
    owner_id, owner_token = await new_user()
    other_id, other_token = await new_user()
    art_id = await new_article(owner_id)
    try:
        async with client(owner_token) as c:
            add_r = await c.post(ADD_URL.format(art_id), json={"folder": "tech"})
            fav_id = add_r.json()["id"]

        async with client(other_token) as c:
            r = await c.patch(PATCH_URL.format(fav_id), json={"folder": "hacked"})

        assert r.status_code == 404, r.text

        # 确认未被篡改
        rows = await _fav_rows(owner_id)
        assert rows[0].folder == "tech"
    finally:
        await _purge(owner_id, other_id)
