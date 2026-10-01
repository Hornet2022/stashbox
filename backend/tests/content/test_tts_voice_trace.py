"""CP-TTS-VOICE 溯源 + 蒸馏完成后的缓存失效（2026-09-30 自由拓展自测回归）。

两个 bug 都在这里：

**BUG#6 —— `distilled_articles.tts_voice_id` 只写不读。**
  蒸馏链路老老实实把「这段音频是谁念的」写进了库，但从头到尾**没有任何一处读它**：
  content-service 的 `ArticleResponse` 没有这个字段，安卓端也没有。
  结果是「换音色重生成」这条闭环只闭环了一半 —— 用户点了重生成，确认框写着
  「将用音色 X」，生成完却无从判断到底换没换（因为任何界面都不显示音色）。

**BUG#7 —— 蒸馏完成后文章详情缓存不失效。**
  蒸馏是 ai-worker 直写 DB，不经过 content-service，所以那份 300s 的
  `article:detail:*` 缓存没人清。实测：改库让蒸馏完成后，
  `GET /articles/{id}` 仍返回 `status=distilling / audio_url=null`，
  而同一时刻 `/status` 返回 `ready` + 正确音频地址。
  用户退出详情页再进来，就会看到一篇 20 分钟前就听完的文章重新显示成「蒸馏中」。

⚠️ 清理放在断言之后（yield fixture）：先删再断言的话，断言挂了数据已经没了，
复现要重来。这是本项目 e2e 的既有教训。

前置：本机 PG + Redis 已起，且已 `alembic upgrade head`（含 0033）。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from helpers import client, new_article, new_task, new_user
from stashbox.backend.common import cache_service
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import (
    Article,
    DistilledArticle,
    TTSVoice,
    User,
    UserTTSPreference,
)
from stashbox.backend.common.tts_voice_service import create_voice


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def voice():
    """一条上架音色，测完删掉。"""
    created = await create_voice(
        slug=f"trace_{uuid.uuid4().hex[:12]}",
        display_name="溯源测试音色",
        ref_audio="/tmp/does-not-need-to-exist.wav",
        ref_text="参考文本",
    )
    vid = created.id
    yield vid
    async with AsyncSessionLocal() as db:
        await db.execute(delete(TTSVoice).where(TTSVoice.id == vid))
        await db.commit()


@pytest.fixture
async def article():
    """一次性用户 + 一篇已就绪文章，测完整条链删干净。

    删除顺序有讲究：articles 的 FK 是 `ON DELETE RESTRICT`（不是 CASCADE），
    所以必须先删文章、再删蒸馏产物、最后删用户 —— 顺序反了会撞
    ForeignKeyViolationError，测试数据就永久留在 dev 库里了。
    """
    uid, token = await new_user()
    aid = await new_article(uid, status="distilling")
    await cache_service.invalidate_article(aid)  # 别让别的用例的缓存串进来
    yield uid, token, aid
    await cache_service.invalidate_article(aid)
    await cache_service.invalidate_pending(uid)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DistilledArticle).where(DistilledArticle.article_id == aid))
        await db.execute(delete(Article).where(Article.id == aid))
        await db.execute(delete(UserTTSPreference).where(UserTTSPreference.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()


# ---------------------------------------------------------------------------
# BUG#6：溯源字段能被读出来
# ---------------------------------------------------------------------------
async def test_详情页回溯源音色(article, voice):
    """详情页要能回答「这段音频是谁念的」。"""
    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=voice)
    await cache_service.invalidate_article(aid)

    async with client(token) as c:
        r = await c.get(f"/api/v1/articles/{aid}")

    assert r.status_code == 200, r.text
    brief = r.json()["tts_voice"]
    assert brief is not None, "详情页没回 tts_voice —— 溯源字段又是只写不读"
    assert brief["id"] == voice
    assert brief["name"] == "溯源测试音色"
    assert brief["available"] is True


async def test_status端点也回溯源音色(article, voice):
    """`/status` 是 App 轮询的那个端点（且不走缓存），重生成后要能立刻看到音色变化。"""
    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=voice)

    async with client(token) as c:
        r = await c.get(f"/api/v1/articles/{aid}/status")

    assert r.status_code == 200, r.text
    assert r.json()["tts_voice"]["id"] == voice


async def test_下架音色仍回名字但标不可用(article, voice):
    """**软删/下架后名字必须留着**。

    这段音频确实是它合成的，藏掉名字等于让历史音频变成「来源不明」，
    用户会以为是自己记错了。所以只把 `available` 打成 false。
    """
    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=voice)
    await cache_service.invalidate_article(aid)

    async with AsyncSessionLocal() as db:
        v = (await db.execute(select(TTSVoice).where(TTSVoice.id == voice))).scalar_one()
        v.is_active = False
        await db.commit()
    await cache_service.invalidate_article(aid)

    async with client(token) as c:
        r = await c.get(f"/api/v1/articles/{aid}")

    brief = r.json()["tts_voice"]
    assert brief is not None, "音色被下架就把历史文章的署名抹掉，是倒退"
    assert brief["name"] == "溯源测试音色"
    assert brief["available"] is False


async def test_软删音色后仍能溯源(article, voice):
    """已软删（deleted_at 非空）的音色同样要能查到署名。

    这里的查询刻意不过滤软删 —— 用 list_voices 那套过滤的话，
    管理员删一次音色，所有历史文章的来源就一起消失了。
    """
    from stashbox.backend.common.tts_voice_service import delete_voice

    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=voice)
    await delete_voice(voice)  # 软删
    await cache_service.invalidate_article(aid)

    async with client(token) as c:
        r = await c.get(f"/api/v1/articles/{aid}")

    brief = r.json()["tts_voice"]
    assert brief is not None and brief["name"] == "溯源测试音色"
    assert brief["available"] is False


async def test_无溯源的旧文章回null(article):
    """迁移前的历史文章本就无从得知，回 null 而不是编一个音色。"""
    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=None)
    await cache_service.invalidate_article(aid)

    async with client(token) as c:
        r = await c.get(f"/api/v1/articles/{aid}")

    assert r.json()["tts_voice"] is None


async def test_列表不逐条查音色(article, voice):
    """列表恒回 null：每行挂个音色名是噪音，而且要为此多 join 一次音色表。

    这条是**钉住现状**的测试 —— 防止以后有人「顺手」在列表里也解析一遍，
    悄悄引入 N+1。
    """
    _, token, aid = article
    await new_task(aid, status="done", audio_url="http://x/a.wav", tts_voice_id=voice)
    await cache_service.invalidate_pending(article[0])

    async with client(token) as c:
        r = await c.get("/api/v1/articles?limit=20")

    assert r.status_code == 200, r.text
    mine = [i for i in r.json()["items"] if i["id"] == aid]
    assert mine, "列表里找不到刚建的探针文章"
    assert all(i["tts_voice"] is None for i in mine)
