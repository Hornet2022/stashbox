"""CP-TTS-VOICE BUG#7：蒸馏落库后必须失效 content-service 的文章详情缓存。

**bug**：蒸馏是 ai-worker 直写 PostgreSQL，不经过 content-service，所以
`GET /api/v1/articles/{id}` 那份 300s 的 `article:detail:*` 缓存从来没人清。
实测（2026-09-30 自测）：

    改库让蒸馏完成（status=done + audio_url）后
      GET /articles/{id}          -> status=distilling, audio_url=null   ← 陈旧
      GET /articles/{id}/status   -> status=ready,     audio_url=...     ← 正确

用户侧的表现：App 轮询 `/status`（不走缓存）看到 ready，音频能播；
但退出详情页再进来时 `loadArticle()` 走详情接口，拿到缓存里的
「蒸馏中 / 无音频」—— 一篇 20 分钟前就听完的文章重新显示成蒸馏中，
还得再轮询 90 秒才恢复。

修法：在 `_persist_agent_final` 落库后 `invalidate_article` + `invalidate_pending`。

⚠️ 这里用 **spy 而不是真 Redis**：`tests/ai/conftest.py` 有个 autouse fixture
`_refund_quota_no_redis_lock`，为了测退款锁把 `redis.asyncio.Redis` 整个换成了
no-op 桩（任何命令都静默成功）。在这个目录里写「先塞缓存再断言缓存没了」，
断言会因为桩而不成立 —— 看着绿，其实什么都没验。
真 Redis 的端到端行为已手工验证过（落库后重读详情拿到的是新 audio_url）。

前置：本机 PG 已起，且已 `alembic upgrade head`（含 0033）。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from stashbox.backend.common import cache_service
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import (
    Article,
    DistilledArticle,
    Feedback,
    User,
    UserTTSPreference,
)


@pytest_asyncio.fixture
async def distilling_article():
    """用户 + 一篇处于 distilling 的文章。

    清理顺序：feedback → distilled_articles → articles → user。
    articles.user_id 与 feedback.user_id 都是 RESTRICT 外键，顺序反了会撞
    ForeignKeyViolationError，测试数据就永久留在 dev 库里。
    """
    async with AsyncSessionLocal() as db:
        user = User(open_id="cp_bug7_" + uuid.uuid4().hex[:20], nickname="pytest", tier="free")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid, aid = int(user.id), f"art_{uuid.uuid4().hex[:24]}"

        db.add(
            Article(
                id=aid,
                user_id=uid,
                url="https://example.com/x",
                source="wechat_mp",
                title="BUG#7 探针",
                status="distilling",
                favorite=False,
                skip=False,
            )
        )
        db.add(
            DistilledArticle(id=f"dst_{uuid.uuid4().hex[:24]}", article_id=aid, status="running")
        )
        await db.commit()

    yield uid, aid

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Feedback).where(Feedback.user_id == uid))
        await db.execute(delete(DistilledArticle).where(DistilledArticle.article_id == aid))
        await db.execute(delete(Article).where(Article.id == aid))
        await db.execute(delete(UserTTSPreference).where(UserTTSPreference.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()


@pytest.fixture
def cache_spy(monkeypatch):
    """记录 `invalidate_article` / `invalidate_pending` 的调用。"""
    calls: list[tuple[str, object]] = []

    async def _fake_invalidate_article(article_id: str) -> None:
        calls.append(("article", article_id))

    async def _fake_invalidate_pending(user_id: int) -> None:
        calls.append(("pending", user_id))

    monkeypatch.setattr(cache_service, "invalidate_article", _fake_invalidate_article)
    monkeypatch.setattr(cache_service, "invalidate_pending", _fake_invalidate_pending)
    return calls


DONE = {
    "status": "done",
    "tts_audio_url": "http://example.com/after.wav",
    "tts_duration_sec": 42,
    "tts_voice_id": None,
    "rewritten_script": "探针正文",
}


async def test_蒸馏落库后失效文章详情缓存(distilling_article, cache_spy):
    """落库函数跑完必须失效详情缓存 —— 否则详情接口会一直吐「蒸馏中」。"""
    from tasks.distill_task import _persist_agent_final

    uid, aid = distilling_article
    await _persist_agent_final(
        task_id=f"dst_probe_{uuid.uuid4().hex[:8]}",
        article_id=aid,
        user_id=uid,
        final=dict(DONE),
    )

    assert ("article", aid) in cache_spy, (
        "蒸馏完成后没有失效 article:detail 缓存 —— BUG#7 没修好，"
        "用户重进详情页会看到陈旧的「蒸馏中 / 无音频」"
    )
    assert ("pending", uid) in cache_spy, "待听列表缓存也没失效，首页会停在旧状态"


async def test_蒸馏失败也要失效缓存(distilling_article, cache_spy):
    """失败路径同样会改状态（distilling → failed），同样必须失效。

    只在成功分支失效是最容易漏的一半：用户等了一轮看到「蒸馏失败」，
    重进详情页却还是「蒸馏中」，比一直卡住更让人懵。
    """
    from tasks.distill_task import _persist_agent_final

    uid, aid = distilling_article
    await _persist_agent_final(
        task_id=f"dst_probe_{uuid.uuid4().hex[:8]}",
        article_id=aid,
        user_id=uid,
        final={"status": "failed", "error": "探针失败"},
    )

    assert ("article", aid) in cache_spy, "失败路径漏了缓存失效"


async def test_落库本身成功(distilling_article, cache_spy):
    """配套断言：确认落库真的写进去了。

    没有这条，上面两条有可能因为「压根没写」而假绿 —— 缓存当然也没被失效，
    但原因是别的。断言具体字段，才能保证测的是「写成功 + 失效」两件事。
    """
    from tasks.distill_task import _persist_agent_final

    uid, aid = distilling_article
    await _persist_agent_final(
        task_id=f"dst_probe_{uuid.uuid4().hex[:8]}",
        article_id=aid,
        user_id=uid,
        final=dict(DONE),
    )

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(DistilledArticle).where(DistilledArticle.article_id == aid))
        ).scalar_one()
        assert row.status == "done"
        assert row.audio_url == "http://example.com/after.wav"
        assert row.duration_sec == 42
