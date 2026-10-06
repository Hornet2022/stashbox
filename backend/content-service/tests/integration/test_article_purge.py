"""CP-DELETE 硬删除回归测试（锁定 0030 修复的回归）。

背景
----
0024（distillation_evaluations）/ 0026（article_audio_variants）建表时
FK 未声明 ondelete，而 `article_purge.purge_article` 硬删
`distilled_articles` 时被它们阻塞：

    真机复现：DELETE /api/v1/articles/art_85d9f8... → 500
    ForeignKeyViolationError: distillation_evaluations_task_id_fkey

0030 修复后（应用层显式按序 + DB CASCADE/SET NULL 兜底），本测试构造
**全子表都有数据**的文章，断言：

- 不抛 FK 异常（回归直接体现在这里）
- 派生/关联数据被清（articles / distilled / variants / favorites /
  later_listens / listening_statuses / push_notifications）
- 分析类数据保留并断引用（feedback_v2.article_id → NULL，
  distillation_evaluations.task_id → NULL）

依赖真 PG（5432）：DB 不在跑会直接报错，不静默跳过（写库语义测不准）。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

# article_purge.py 在 content-service/ 下（目录名带连字符，不可当包 import）
CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_cp_purge_under_test", CONTENT_SERVICE_DIR / "article_purge.py"
)
purge_mod = importlib.util.module_from_spec(_spec)
sys.modules["_cp_purge_under_test"] = purge_mod
_spec.loader.exec_module(purge_mod)

from stashbox.backend.common.models import (  # noqa: E402
    Article,
    ArticleAudioVariant,
    DistilledArticle,
    DistillationEvaluation,
    Favorite,
    FeedbackV2,
    LaterListen,
    ListeningStatus,
    PushNotification,
)

TEST_USER_ID = 6892  # 见 user_row fixture：测试自己保证这一行存在
TEST_SOURCE = "purge_test"  # 本测试在 articles 表的命名空间（便于清理）

# 本文件建过的 article_id 全集。purge 的设计意图是**保留**反馈与分析数据
# （只断引用），所以这些 FeedbackV2 行在用例结束时仍然存在、且仍引用
# TEST_USER_ID —— user_row 想删用户行就得先按精确 id 把它们清掉。
# 用 id 而非业务值匹配，和本文件 _cleanup 的原则一致（不误删同值行）。
_CREATED_ARTICLE_IDS: set[str] = set()


@pytest.fixture
async def user_row():
    """保证 TEST_USER_ID 那一行 users 存在，用例结束后复原。

    原来是注释里那句「dev 联调账号，测试库必然存在」——那是个**关于某个
    本机数据库状态的假设**，不是代码保证。放进 CI（全新 postgres）后这个假设
    立刻不成立：`articles_user_id_fkey` 报 ForeignKeyViolationError，
    整条 purge 回归路径直接测不了。

    这正是这批用例长期不在 CI 里的后果：它们靠本机环境的隐式前提才能跑。
    显式建行 + 复原后，用例只依赖代码，不依赖谁的机器上有什么数据。
    """
    from sqlalchemy import select as _select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import User

    async with AsyncSessionLocal() as db:
        existed = await db.scalar(_select(User).where(User.id == TEST_USER_ID))
        created = existed is None
        if created:
            db.add(User(id=TEST_USER_ID, nickname="purge 测试账号"))
            await db.commit()

    try:
        yield TEST_USER_ID
    finally:
        # 只删自己建的那一行。已存在的（dev 库里的联调账号）原样保留 ——
        # 测试不该动它，更不该因为自己需要它就把它删了。
        if created:
            async with AsyncSessionLocal() as db:
                # purge 刻意保留的反馈行仍引用着这个 user，先按精确 id 清掉，
                # 否则 feedback_v2_user_id_fkey 会让删用户行直接失败。
                if _CREATED_ARTICLE_IDS:
                    await db.execute(
                        delete(FeedbackV2).where(
                            FeedbackV2.user_id == TEST_USER_ID,
                            FeedbackV2.article_id.in_(_CREATED_ARTICLE_IDS),
                        )
                    )
                await db.execute(delete(User).where(User.id == TEST_USER_ID))
                await db.commit()
        _CREATED_ARTICLE_IDS.clear()


def _rid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


async def _seed_full_graph(db, article_id: str, task_id: str) -> dict[str, str]:
    """建一篇文章 + 填满所有子表（purge 需要覆盖的每条 FK 路径）。

    分两段 commit：models 之间没有 ORM relationship（只有 FK 列），
    SQLAlchemy 不保证 `distilled_articles` 先于 `article_audio_variants`
    flush，先落父行避免测试自身踩 FK。

    返回本测试建的行 id，供 [_cleanup] **精确**清理 —— 不可用
    "值匹配"（如 score/hook 组合）兜底，那会误删生产里同值的行。
    """
    eval_id = _rid("eval")
    variant_id = _rid("avar")
    _CREATED_ARTICLE_IDS.add(article_id)
    db.add(
        Article(
            id=article_id,
            user_id=TEST_USER_ID,
            url=f"https://example.com/{article_id}",
            title="purge 回归测试",
            source=TEST_SOURCE,
            status="ready",
        )
    )
    db.add(
        DistilledArticle(
            id=task_id,
            article_id=article_id,
            status="done",
            audio_url=f"https://example.com/{article_id}.wav",
            duration_sec=100,
        )
    )
    await db.commit()  # 父行先落

    # 派生物：应随产物删除
    db.add(
        ArticleAudioVariant(
            id=variant_id,
            distilled_article_id=task_id,
            bitrate=64,
            file_size_bytes=800_000,
            oss_key=f"audio/{article_id}.64k.m4a",
            duration_sec=100,
        )
    )
    # 主观分析数据：应保留、仅断引用
    db.add(
        DistillationEvaluation(
            id=eval_id,
            task_id=task_id,
            user_id=TEST_USER_ID,
            overall_score=4,
            hook_score=5,
        )
    )
    db.add(Favorite(user_id=TEST_USER_ID, article_id=article_id, folder="default"))
    db.add(LaterListen(user_id=TEST_USER_ID, article_id=article_id))
    db.add(ListeningStatus(user_id=TEST_USER_ID, article_id=article_id, position_sec=30))
    db.add(
        FeedbackV2(
            user_id=TEST_USER_ID,
            article_id=article_id,
            category="bug",
            content="purge 回归测试-反馈应保留",
        )
    )
    # DB CASCADE 路径
    db.add(
        PushNotification(
            user_id=TEST_USER_ID,
            article_id=article_id,
            title="purge 回归测试",
            body="body",
        )
    )
    await db.commit()
    return {"eval_id": eval_id, "variant_id": variant_id}


async def _cleanup(db, article_id: str, task_id: str, ids: dict[str, str]) -> None:
    """兜底清理（用例失败时也能收干净；正常路径下这些行已被 purge）。

    只删**本测试明确建过的 id / article_id / task_id**，
    绝不按业务值匹配（避免误删生产同值行）。
    """
    for model, col in (
        (PushNotification, PushNotification.article_id),
        (ListeningStatus, ListeningStatus.article_id),
        (LaterListen, LaterListen.article_id),
        (Favorite, Favorite.article_id),
        (FeedbackV2, FeedbackV2.article_id),
    ):
        rows = (await db.execute(select(model).where(col == article_id))).scalars().all()
        for r in rows:
            await db.delete(r)
    # 断引用后的分析数据：按本次生成的 eval_id 精确删
    ev = (
        await db.execute(
            select(DistillationEvaluation).where(DistillationEvaluation.id == ids["eval_id"])
        )
    ).scalar_one_or_none()
    if ev is not None:
        await db.delete(ev)
    # 变体：按本次生成的 variant_id 精确删
    av = (
        await db.execute(
            select(ArticleAudioVariant).where(ArticleAudioVariant.id == ids["variant_id"])
        )
    ).scalar_one_or_none()
    if av is not None:
        await db.delete(av)
    art = (await db.execute(select(Article).where(Article.id == article_id))).scalar_one_or_none()
    if art is not None:
        await db.delete(art)
    ds = (
        await db.execute(select(DistilledArticle).where(DistilledArticle.id == task_id))
    ).scalar_one_or_none()
    if ds is not None:
        await db.delete(ds)
    await db.commit()


async def test_purge_full_graph_does_not_raise_and_clears_children(db_setup, user_row):
    """全子表文章删除：不抛异常 + 关联清空 + 分析数据保留断引用。"""
    db = db_setup
    article_id = _rid("art")
    task_id = _rid("dst")

    ids = await _seed_full_graph(db, article_id, task_id)
    try:
        # ── 回归点：0030 前这里抛 ForeignKeyViolationError ──
        art = await purge_mod.purge_article(db, article_id)
        await db.commit()
        assert art is not None, "purge_article 应返回被删的 Article"

        # 1. 本体与派生数据清空
        assert (
            await db.execute(select(Article).where(Article.id == article_id))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(DistilledArticle).where(DistilledArticle.id == task_id))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(
                select(ArticleAudioVariant).where(
                    ArticleAudioVariant.distilled_article_id == task_id
                )
            )
        ).scalar_one_or_none() is None

        # 2. 关联表清空（含 push_notifications 的 DB CASCADE）
        assert (
            await db.execute(select(Favorite).where(Favorite.article_id == article_id))
        ).scalars().all() == []
        assert (
            await db.execute(select(LaterListen).where(LaterListen.article_id == article_id))
        ).scalars().all() == []
        assert (
            await db.execute(
                select(ListeningStatus).where(ListeningStatus.article_id == article_id)
            )
        ).scalars().all() == []
        assert (
            await db.execute(
                select(PushNotification).where(PushNotification.article_id == article_id)
            )
        ).scalars().all() == []

        # 3. 分析数据保留 + 断引用
        ev = (
            (
                await db.execute(
                    select(DistillationEvaluation).where(
                        DistillationEvaluation.user_id == TEST_USER_ID,
                        DistillationEvaluation.overall_score == 4,
                        DistillationEvaluation.task_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert ev, "评分应保留（task_id 置 NULL）"

        fb = (
            (
                await db.execute(
                    select(FeedbackV2).where(
                        FeedbackV2.content.like("%purge 回归测试-反馈应保留%"),
                        FeedbackV2.article_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert fb, "反馈应保留（article_id 置 NULL）"
    finally:
        await _cleanup(db, article_id, task_id, ids)


async def test_purge_missing_article_returns_none(db_setup, user_row):
    """幂等：删不存在的文章返回 None，不抛异常（越权/重复删走 404 语义）。"""
    missing = _rid("art")
    result = await purge_mod.purge_article(db_setup, missing)
    assert result is None
