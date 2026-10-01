"""CP3.7.3：PostDistillHook 完整实现单测（AutoRetry + FewShotPool + ListeningPattern + ScorePredict）。

16 个用例覆盖：
- few_shot_pool：score 4 入池 / score 3 跳过 / Levenshtein 查重 / LRU 1000 / user pool 优先
- listening_pattern_updater：feedback_count 累加 / 加权平均 / 冷启动保护 / 失败兜底
- AutoRetryHook：score 2 触发 / score 3 跳过 / 限流 / 关闭开关
- ScorePredictorHook：mock 返回 8.5
- Pipeline 集成：default_post_hooks 返回 4 个
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    MetaData,
    String as SA_String,
    Table,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))

from stashbox.backend.common.models import (  # noqa: E402
    ArticleAudioVariant,
    DistillationEvaluation,
    DistilledArticle,
    FewShotExample,
    UserListeningPattern,
)

# FK 骨架表（独立 MetaData）
_FK_METADATA = MetaData()
_users_stub = Table(
    "users",
    _FK_METADATA,
    Column("id", SA_String(32), primary_key=True),
)


# 2026-10-02：不再手写影子表。hook 改成经
# article_id → DistilledArticle.id → evaluations.task_id 三段跳之后，
# 测试要真的往 distilled_articles 插一行，影子表必须跟着 ORM 一起长 ——
# 手写版本就是因为漏了 script_text 等列反复报 no such column。
# 这里直接复用真实模型的列定义，但剥掉外键（fixture 里没有 articles /
# tts_voices 两张表，SQLite 建表时外键会因找不到目标表而失败）。
def _sqlite_safe_columns(table) -> list:
    """把 ORM 表的列定义搬进 SQLite fixture。

    两处不得不改：
    - JSONB 在 SQLite 上没有编译器（visit_JSONB 不存在），降级成 JSON；
    - 外键指向的 articles / tts_voices 表不在 fixture 里，会导致建表失败，
      所以逐列 copy 时不复制 ForeignKey 约束。
    """
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB

    cols = []
    for c in table.columns:
        new_c = c.copy()
        if isinstance(new_c.type, JSONB):
            new_c.type = JSON()
        new_c.foreign_keys = set()
        cols.append(new_c)
    return cols


_distilled_articles_stub = Table(
    "distilled_articles",
    _FK_METADATA,
    *_sqlite_safe_columns(DistilledArticle.__table__),
)


@pytest.fixture
async def session():
    """CP3.7.3 测试 fixture：aiosqlite 异步引擎 + AsyncSession。

    SQLite 兼容：所有 INSERT 显式提供 created_at / updated_at / last_updated。
    ORM 默认 server_default='now()' 在 PG 有效，SQLite 不识别，
    但本测试不依赖 default —— 显式给字段值。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        for table in [
            _users_stub,
            _distilled_articles_stub,
            DistillationEvaluation.__table__,
            ArticleAudioVariant.__table__,
            UserListeningPattern.__table__,
            FewShotExample.__table__,
        ]:
            await conn.run_sync(lambda sync_conn, t=table: t.create(sync_conn, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. few_shot_pool：score 4 入池
# ---------------------------------------------------------------------------
async def test_add_high_score_to_pool_score_4_inserts(session):
    """CP3.7.3 §2.1.D：overall_score=4 → 入池。"""
    from distill.few_shot_pool import add_high_score_to_pool

    now = datetime.now()
    ev = DistillationEvaluation(
        id="eval_1",
        task_id="dst_1",
        user_id=1,
        overall_score=4,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.commit()

    result = await add_high_score_to_pool(session, ev, "高分改写文本", "hook", user_id=None)
    await session.commit()
    assert result is not None
    assert result.rewrite_text == "高分改写文本"
    assert result.score_avg == 4.0


# ---------------------------------------------------------------------------
# 2. few_shot_pool：score 3 跳过
# ---------------------------------------------------------------------------
async def test_add_high_score_to_pool_score_3_skips(session):
    """CP3.7.3：overall_score=3 (< 4 门槛) → 跳过。"""
    from distill.few_shot_pool import add_high_score_to_pool

    now = datetime.now()
    ev = DistillationEvaluation(
        id="eval_2",
        task_id="dst_2",
        user_id=1,
        overall_score=3,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.commit()

    result = await add_high_score_to_pool(session, ev, "改写文本", "hook")
    await session.commit()
    assert result is None


# ---------------------------------------------------------------------------
# 3. few_shot_pool：Levenshtein 查重
# ---------------------------------------------------------------------------
async def test_add_high_score_to_pool_levenshtein_dedup(session):
    """CP3.7.3：相似文本（Levenshtein 距离 < 0.1）→ 跳过。"""
    from distill.few_shot_pool import add_high_score_to_pool

    now = datetime.now()
    ev1 = DistillationEvaluation(
        id="eval_d1",
        task_id="dst_d1",
        user_id=1,
        overall_score=4,
        created_at=now,
        updated_at=now,
    )
    session.add(ev1)
    await session.commit()

    await add_high_score_to_pool(session, ev1, "AI 改变了我们的生活", "hook")
    await session.commit()

    ev2 = DistillationEvaluation(
        id="eval_d2",
        task_id="dst_d2",
        user_id=1,
        overall_score=4,
        created_at=now,
        updated_at=now,
    )
    session.add(ev2)
    await session.commit()

    result = await add_high_score_to_pool(session, ev2, "AI 改变了我们的生活", "hook")
    await session.commit()
    assert result is None


# ---------------------------------------------------------------------------
# 4. levenshtein helper
# ---------------------------------------------------------------------------
def test_levenshtein_helper():
    """CP3.7.3：_is_similar_text helper 行为正确。"""
    from distill.few_shot_pool import _is_similar_text, _levenshtein_distance

    assert _levenshtein_distance("hello", "hello") == 0
    assert _levenshtein_distance("hello", "hallo") == 1
    assert _levenshtein_distance("abc", "xyz") == 3
    assert _is_similar_text("AI 改变了生活", "AI 改变了生活") is True
    assert _is_similar_text("hello world", "goodbye world") is False
    assert _is_similar_text("", "test") is False


# ---------------------------------------------------------------------------
# 5. select_few_shot：user pool 优先
# ---------------------------------------------------------------------------
async def test_select_few_shot_user_pool_first(session):
    """CP3.7.3 §2.1.D：先取 user_id 个人池。"""
    from distill.few_shot_pool import add_high_score_to_pool, select_few_shot

    now = datetime.now()
    import uuid as _uuid

    for i in range(3):
        ev = DistillationEvaluation(
            id=f"eval_u{i}",
            task_id=f"dst_u{i}",
            user_id=100,
            overall_score=4,
            created_at=now,
            updated_at=now,
        )
        session.add(ev)
        await session.commit()
        # 用 uuid 确保文本完全不同（避免 Levenshtein 查重误判）
        await add_high_score_to_pool(
            session,
            ev,
            f"个人池unique_{_uuid.uuid4()}_test_only_no_similarity_其它字符",
            "hook",
            user_id=100,
        )
    for i in range(2):
        ev = DistillationEvaluation(
            id=f"eval_g{i}",
            task_id=f"dst_g{i}",
            user_id=200,
            overall_score=4,
            created_at=now,
            updated_at=now,
        )
        session.add(ev)
        await session.commit()
        await add_high_score_to_pool(
            session,
            ev,
            f"全局池unique_{_uuid.uuid4()}_test_only_no_similarity_global",
            "hook",
            user_id=None,
        )
    await session.commit()

    examples = await select_few_shot(session, user_id=100, kind="hook", limit=5)
    assert len(examples) == 5
    personal = [e for e in examples if e.user_id == 100]
    assert len(personal) == 3
    global_pool = [e for e in examples if e.user_id is None]
    assert len(global_pool) == 2


# ---------------------------------------------------------------------------
# 6. listening_pattern_updater：feedback_count 累加
# ---------------------------------------------------------------------------
async def test_update_user_listening_pattern_increments_count(session):
    """CP3.7.3 §2.1.B：feedback_count += 1。"""
    from distill.listening_pattern_updater import update_user_listening_pattern

    now = datetime.now()
    pat = UserListeningPattern(
        user_id=1,
        feedback_count=10,
        last_updated=now,
        created_at=now,
        updated_at=now,
    )
    session.add(pat)
    await session.commit()

    ev = DistillationEvaluation(
        id="eval_lp1",
        task_id="dst_lp1",
        user_id=1,
        overall_score=4,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.commit()

    result = await update_user_listening_pattern(session, 1, ev)
    await session.commit()
    assert result is not None
    assert result.feedback_count == 11


# ---------------------------------------------------------------------------
# 7. listening_pattern_updater：加权平均
# ---------------------------------------------------------------------------
async def test_update_user_listening_pattern_weighted_avg(session):
    """CP3.7.3：avg_overall_score = 0.3 * 旧 + 0.7 * 新。"""
    from distill.listening_pattern_updater import update_user_listening_pattern

    now = datetime.now()
    pat = UserListeningPattern(
        user_id=1,
        feedback_count=10,
        avg_overall_score=4.0,
        last_updated=now,
        created_at=now,
        updated_at=now,
    )
    session.add(pat)
    await session.commit()

    ev = DistillationEvaluation(
        id="eval_lp2",
        task_id="dst_lp2",
        user_id=1,
        overall_score=5,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.commit()

    result = await update_user_listening_pattern(session, 1, ev)
    await session.commit()
    assert result.avg_overall_score is not None
    assert abs(result.avg_overall_score - 4.7) < 0.01


# ---------------------------------------------------------------------------
# 8. listening_pattern_updater：冷启动保护
# ---------------------------------------------------------------------------
async def test_update_user_listening_pattern_cold_start_under_5(session):
    """CP3.7.3：feedback_count < 5 时画像保持 NULL。"""
    from distill.listening_pattern_updater import update_user_listening_pattern

    now = datetime.now()
    ev = DistillationEvaluation(
        id="eval_cs1",
        task_id="dst_cs1",
        user_id=2,
        overall_score=5,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.commit()

    result = await update_user_listening_pattern(session, 2, ev)
    await session.commit()
    assert result is not None
    assert result.feedback_count == 1
    assert result.avg_overall_score is None
    assert result.preferred_rhythm is None


# ---------------------------------------------------------------------------
# 9. listening_pattern_updater：失败兜底
# ---------------------------------------------------------------------------
async def test_update_user_listening_pattern_failure_continues(monkeypatch):
    """CP3.7.3：异常 → log warning + return None，不破主流程。"""

    class _BrokenSession:
        def scalar(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

        async def flush(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

    from distill.listening_pattern_updater import update_user_listening_pattern

    ev = DistillationEvaluation(id="eval_fail", task_id="dst_fail", user_id=1, overall_score=4)
    result = await update_user_listening_pattern(_BrokenSession(), 1, ev)
    assert result is None


# ---------------------------------------------------------------------------
# 10-12. AutoRetryHook
# ---------------------------------------------------------------------------
def test_auto_retry_score_2_triggers():
    """CP3.7.3：score=2, count=0 → 触发重蒸。"""
    from distill.auto_retry import should_auto_retry

    assert should_auto_retry(2.0, 0) is True


def test_auto_retry_score_3_skips():
    """CP3.7.3：score > 2 → 不触发。"""
    from distill.auto_retry import should_auto_retry

    assert should_auto_retry(3.0, 0) is False
    assert should_auto_retry(5.0, 0) is False


def test_auto_retry_rate_limit():
    """CP3.7.3：user_daily_retry_count >= 3 → 限流。"""
    from distill.auto_retry import should_auto_retry

    assert should_auto_retry(2.0, 3) is False
    assert should_auto_retry(2.0, 5) is False
    assert should_auto_retry(2.0, 2) is True


def test_auto_retry_disabled_no_op(monkeypatch):
    """CP3.7.3：env=false → 全部 no-op。"""
    monkeypatch.setenv("AUTO_RETRY_ENABLED", "false")
    from distill.auto_retry import should_auto_retry

    assert should_auto_retry(2.0, 0) is False


# ---------------------------------------------------------------------------
# 13-14. ScorePredictorHook
# ---------------------------------------------------------------------------
async def test_score_predictor_no_fake_score_constant():
    """2026-10-02：MOCK_SCORE 常量已移除 —— 系统不再编造质量分。

    写死 8.5 比留 NULL 更有害：NULL 读作「未评估」，8.5 读作「评估过、质量好」，
    管理后台评分列 / A/B 报表都会拿它当依据。
    """
    import distill.score_predictor as sp

    assert not hasattr(sp, "MOCK_SCORE"), "MOCK_SCORE 不该再存在"


async def test_score_predictor_no_real_eval_returns_none():
    """2026-10-02：查库失败 / 无真实评分 → 返回 None，绝不写假分。"""
    from distill.score_predictor import predict_and_save_quality_score

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated db error")

    result = await predict_and_save_quality_score(_BrokenSession(), "art_x")
    assert result is None


async def test_score_predictor_empty_evaluations_returns_none():
    """2026-10-02：真实评分表为空 → None（不是 8.5）。"""
    from distill.score_predictor import _real_score_from_evaluations

    class _EmptySession:
        async def execute(self, *args, **kwargs):
            class _R:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

            return _R()

    assert await _real_score_from_evaluations(_EmptySession(), "art_x") is None


async def test_score_predictor_real_eval_averages_to_0_10_scale():
    """2026-10-02：有真实评分 → 均值 ×2 落到 0-10 量纲。"""
    from distill.score_predictor import _real_score_from_evaluations

    class _Session:
        # 第一次 execute 查 DistilledArticle.id（桥接），第二次查评分
        def __init__(self):
            self.calls = 0

        async def scalar(self, *args, **kwargs):
            return "dst_first_task_id"

        async def execute(self, *args, **kwargs):
            class _R:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return [4, 5, 3]

            return _R()

    # (4+5+3)/3 = 4.0 → ×2 = 8.0（0-10 制）
    assert await _real_score_from_evaluations(_Session(), "art_x") == 8.0


async def test_score_predictor_bridges_via_distilled_article_id():
    """2026-10-02：评分只认 task_id，必须经 DistilledArticle 桥接。

    distillation_evaluations 没有 article_id 列；直接按当前 ctx.task_id 查，
    文章重蒸后就对不上了（表现为「用户打过 1 星，系统毫无反应」）。
    """
    from distill.score_predictor import _real_score_from_evaluations

    seen: list[str] = []

    class _Session:
        async def scalar(self, stmt, *args, **kwargs):
            seen.append("scalar:DistilledArticle.id")
            return "dst_first_task_id"

        async def execute(self, stmt, *args, **kwargs):
            seen.append("execute:evaluations")
            sql = str(stmt)
            # SQLAlchemy 2.0 风格：值编译进 statement 的绑定参数里，不是 kwargs
            bound = stmt.compile().params

            class _R:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return [2]

            assert "distillation_evaluations" in sql
            assert "dst_first_task_id" in bound.values(), f"绑定参数里没有桥接 id: {bound}"
            return _R()

    # 2 星 → ×2 = 4.0
    assert await _real_score_from_evaluations(_Session(), "art_x") == 4.0
    assert seen == ["scalar:DistilledArticle.id", "execute:evaluations"]


async def test_score_predictor_no_distilled_row_returns_none():
    """2026-10-02：没有蒸馏结果行 → None，不是 8.5。"""
    from distill.score_predictor import _real_score_from_evaluations

    class _Session:
        async def scalar(self, *args, **kwargs):
            return None

        async def execute(self, *args, **kwargs):
            raise AssertionError("没有 DistilledArticle 行时不该再查评分")

    assert await _real_score_from_evaluations(_Session(), "art_x") is None


# ---------------------------------------------------------------------------
# 15. Pipeline 集成
# ---------------------------------------------------------------------------
def test_default_post_hooks_returns_4_hooks_cp373():
    """CP3.7.3：default_post_hooks 返回 4 个（ScorePredictor + AutoRetry + ListeningPattern + FewShotPool）。"""
    from distill.hooks_impl import (
        AutoRetryHook,
        FewShotPoolHook,
        ListeningPatternUpdaterHook,
        ScorePredictorHook,
        default_post_hooks,
    )

    hooks = default_post_hooks()
    assert len(hooks) == 4
    assert isinstance(hooks[0], ScorePredictorHook)
    assert isinstance(hooks[1], AutoRetryHook)
    assert isinstance(hooks[2], ListeningPatternUpdaterHook)
    assert isinstance(hooks[3], FewShotPoolHook)


# ---------------------------------------------------------------------------
# 16. post-hook 不得伪造评分（CP3.8.0 清理）
# ---------------------------------------------------------------------------
# 背景：这几个 hook 早期版本 `DistillationEvaluation(overall_score=4)` 造一条
# 假评分再喂画像 / few-shot 池 / 重蒸判定。真机数据上表现为
# distillation_evaluations 全是 score=4 的水货，而用户真实打的 1 星被稀释；
# feedback_count 随蒸馏次数增长，冷启动保护（<5）形同虚设。
# 下面 4 个用例锁住"没有真实评分就不动"。
def _ctx(task_id="dst_hook", user_id=1, hook="真实改写的开场句"):
    from distill.schemas import DistillContext, RewriteOutput

    return DistillContext(
        task_id=task_id,
        article_id=f"art_{task_id}",
        user_id=user_id,
        url="https://example.com",
        raw_content="原文正文",
        rewrite=RewriteOutput(hook=hook, sections=["正文"], outro="结尾"),
    )


async def test_hooks_do_not_fabricate_evaluation_when_none_exists(session):
    """没有真实评分 → 三个 hook 都不写表、不建画像、不入池。"""
    from sqlalchemy import func, select

    from distill.hooks_impl import (
        AutoRetryHook,
        FewShotPoolHook,
        ListeningPatternUpdaterHook,
    )

    ctx = _ctx()
    for hook in (AutoRetryHook(), ListeningPatternUpdaterHook(), FewShotPoolHook()):
        await hook(ctx, session)
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(DistillationEvaluation)) == 0
    assert await session.scalar(select(func.count()).select_from(UserListeningPattern)) == 0
    assert await session.scalar(select(func.count()).select_from(FewShotExample)) == 0


async def test_hooks_use_real_evaluation_when_exists(session):
    """用户真打了 5 星 → 画像更新 + 入池，且只依据这条真评分。"""
    from sqlalchemy import func, select

    from distill.hooks_impl import FewShotPoolHook, ListeningPatternUpdaterHook

    now = datetime.now()
    # 2026-10-02：hook 改成 article_id → DistilledArticle.id → evaluations.task_id
    # 三段跳，所以必须先有蒸馏结果行，否则查不到评分（这正是修复要覆盖的场景）。
    session.add(DistilledArticle(id="dst_hook", article_id="art_dst_hook", status="done"))
    session.add(
        DistillationEvaluation(
            id="eval_real_1",
            task_id="dst_hook",
            user_id=1,
            overall_score=5,
            auto_flag=False,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()

    ctx = _ctx()
    await ListeningPatternUpdaterHook()(ctx, session)
    await FewShotPoolHook()(ctx, session)
    await session.commit()

    # 评分行仍是原来那 1 条（hook 不再自己造第 2 条）
    assert await session.scalar(select(func.count()).select_from(DistillationEvaluation)) == 1
    pat = await session.scalar(
        select(UserListeningPattern).where(UserListeningPattern.user_id == 1)
    )
    assert pat is not None
    assert pat.feedback_count == 1
    ex = await session.scalar(select(func.count()).select_from(FewShotExample))
    assert ex == 1


async def test_few_shot_pool_hook_ignores_auto_flag_evaluation(session):
    """auto_flag=true（自动/评测员评分）不算用户信号，不入池。"""
    from sqlalchemy import func, select

    from distill.hooks_impl import FewShotPoolHook, ListeningPatternUpdaterHook

    now = datetime.now()
    session.add(
        DistillationEvaluation(
            id="eval_auto_1",
            task_id="dst_hook",
            user_id=1,
            overall_score=5,
            auto_flag=True,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()

    ctx = _ctx()
    await ListeningPatternUpdaterHook()(ctx, session)
    await FewShotPoolHook()(ctx, session)
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(FewShotExample)) == 0
    assert await session.scalar(select(func.count()).select_from(UserListeningPattern)) == 0


async def test_few_shot_pool_hook_never_inserts_default_text(session):
    """hook 文本为空时跳过，绝不把字面量 "default text" 塞进池子喂给 LLM。"""
    from sqlalchemy import func, select

    from distill.hooks_impl import FewShotPoolHook

    now = datetime.now()
    session.add(
        DistillationEvaluation(
            id="eval_real_2",
            task_id="dst_hook",
            user_id=1,
            overall_score=5,
            auto_flag=False,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()

    await FewShotPoolHook()(_ctx(hook=""), session)
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(FewShotExample)) == 0
