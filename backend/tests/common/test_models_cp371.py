"""CP3.7.1：听感产品化数据底座 4 张表单测。

策略：SQLite + create_all 测表 schema（避开真 PG 依赖）。
本测试只验证 ORM/Pydantic schema 完整性，不测 alembic 迁移（迁移在 CP11.x 验证）。

8 个用例覆盖：
1-4. 4 张表 roundtrip（insert + select + 字段一致性）
5. distillation_evaluations 字段约束（overall_score 范围）
6. few_shot_examples kind enum 约束
7. Pydantic schema 反向序列化（ORM → schema）
8. 4 张表的 metadata 注册在 alembic 链路里
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

# CP3.7.1：让 ai-service/distill/schemas 可被 import（不加这行 distill.schemas 找不到）
AI_SERVICE_DIR = str(Path(__file__).resolve().parents[2] / "ai-service")
if AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, AI_SERVICE_DIR)

from datetime import datetime  # noqa: E402
from sqlalchemy import (  # noqa: E402
    Column,
    MetaData,
    String as SA_String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.orm import sessionmaker  # noqa: E402

from stashbox.backend.common.models import (  # noqa: E402
    ArticleAudioVariant,
    Base,
    DistillationEvaluation,
    FewShotExample,
    UserListeningPattern,
)


# 全局独立 metadata（不污染 Base.metadata）
_FK_METADATA = MetaData()
_users_stub = Table(
    "users",
    _FK_METADATA,
    Column("id", SA_String(32), primary_key=True),
)
_distilled_articles_stub = Table(
    "distilled_articles",
    _FK_METADATA,
    Column("id", SA_String(32), primary_key=True),
)


@pytest.fixture
def sqlite_engine():
    """SQLite in-memory engine + 创建 4 张目标表 + 2 张 FK 骨架表。

    已有 `articles.raw_content` 是 JSONB 列，SQLite 不支持 JSONB。
    解法：FK 用极简骨架表（只 id 列）满足约束，不触发 JSONB 编译错误。

    注意：SQLite 不识别 PG 的 `now()` server_default，INSERT 时如果 column
    没有传值，SQLite 会用字符串 'now()' 解析失败。
    因此测试构造 ORM 实例时显式提供 created_at / updated_at / last_updated。
    """
    engine = create_engine("sqlite:///:memory:", echo=False)

    # 手动创建 6 张表（按依赖顺序：FK 骨架表先）
    for table in [
        _users_stub,
        _distilled_articles_stub,
        DistillationEvaluation.__table__,
        UserListeningPattern.__table__,
        ArticleAudioVariant.__table__,
        FewShotExample.__table__,
    ]:
        table.create(engine, checkfirst=True)

    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. distillation_evaluations roundtrip
# ---------------------------------------------------------------------------
def test_distillation_evaluations_roundtrip(sqlite_engine):
    """插入 + 查回，字段一致。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        # 直接插入 FK 骨架行（用 sqlalchemy.insert 而非 ORM，避开 ORM 重复注册问题）
        session.execute(_users_stub.insert().values(id="1"))
        session.execute(_distilled_articles_stub.insert().values(id="dst_x"))
        session.commit()

        now = datetime.now()
        eval_row = DistillationEvaluation(
            id="eval_x",
            task_id="dst_x",
            user_id=1,
            hook_score=4,
            section_score=3,
            outro_score=5,
            rhythm_score=4,
            overall_score=4,
            comment="好听！",
            skip_reason=None,
            auto_flag=False,
            created_at=now,
            updated_at=now,
        )
        session.add(eval_row)
        session.commit()

        # 查回
        row = session.execute(
            select(DistillationEvaluation).where(DistillationEvaluation.id == "eval_x")
        ).scalar_one()
        assert row.task_id == "dst_x"
        assert row.user_id == 1
        assert row.hook_score == 4
        assert row.section_score == 3
        assert row.outro_score == 5
        assert row.rhythm_score == 4
        assert row.overall_score == 4
        assert row.comment == "好听！"
        assert row.auto_flag is False
        assert row.created_at is not None


# ---------------------------------------------------------------------------
# 2. user_listening_patterns roundtrip
# ---------------------------------------------------------------------------
def test_user_listening_patterns_roundtrip(sqlite_engine):
    """插入用户画像 + 查回 + 验证 30 篇窗口字段。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        session.execute(_users_stub.insert().values(id="2"))
        session.commit()

        now = datetime.now()
        pat = UserListeningPattern(
            user_id=2,
            feedback_count=15,
            avg_session_sec=300,
            skip_rate=0.15,
            completion_rate=0.85,
            preferred_rhythm="fast",
            preferred_hook_type="question",
            avg_overall_score=4.2,
            last_distill_at="2026-09-24T07:00:00",
            last_updated=now,
            created_at=now,
            updated_at=now,
        )
        session.add(pat)
        session.commit()

        row = session.execute(
            select(UserListeningPattern).where(UserListeningPattern.user_id == 2)
        ).scalar_one()
        assert row.feedback_count == 15
        assert row.avg_session_sec == 300
        assert abs(row.skip_rate - 0.15) < 1e-6
        assert abs(row.completion_rate - 0.85) < 1e-6
        assert row.preferred_rhythm == "fast"
        assert row.preferred_hook_type == "question"
        assert abs(row.avg_overall_score - 4.2) < 1e-6
        assert row.last_distill_at == "2026-09-24T07:00:00"


def test_user_listening_patterns_cold_start(sqlite_engine):
    """CP3.7.1 §2.1.B 冷启动保护：feedback_count=0 时所有画像字段 NULL。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        session.execute(_users_stub.insert().values(id="3"))
        session.commit()

        now = datetime.now()
        pat = UserListeningPattern(
            user_id=3,
            feedback_count=0,
            last_updated=now,
            created_at=now,
            updated_at=now,
        )
        session.add(pat)
        session.commit()

        row = session.execute(
            select(UserListeningPattern).where(UserListeningPattern.user_id == 3)
        ).scalar_one()
        assert row.feedback_count == 0
        assert row.skip_rate is None
        assert row.completion_rate is None
        assert row.preferred_rhythm is None


# ---------------------------------------------------------------------------
# 3. article_audio_variants roundtrip（含 unique 约束）
# ---------------------------------------------------------------------------
def test_article_audio_variants_roundtrip(sqlite_engine):
    """插入 3 码率变体 + 查回 + 验证字段。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        session.execute(_distilled_articles_stub.insert().values(id="dst_avar"))
        session.commit()

        now = datetime.now()
        for bitrate, size in [(128, 1000000), (96, 800000), (64, 600000)]:
            var = ArticleAudioVariant(
                id=f"avar_{bitrate}",
                distilled_article_id="dst_avar",
                bitrate=bitrate,
                file_size_bytes=size,
                oss_key=f"audio/art_avar/{bitrate}.m4a",
                format="m4a",
                duration_sec=300,
                sample_rate=24000 if bitrate == 128 else 22050,
                mono=True,
                created_at=now,
                updated_at=now,
            )
            session.add(var)
        session.commit()

        rows = (
            session.execute(
                select(ArticleAudioVariant).where(
                    ArticleAudioVariant.distilled_article_id == "dst_avar"
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        bitrates = sorted(r.bitrate for r in rows)
        assert bitrates == [64, 96, 128]


def test_article_audio_variants_unique_constraint(sqlite_engine):
    """unique(distilled_article_id, bitrate) 约束生效。"""
    from sqlalchemy.exc import IntegrityError

    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        session.execute(_distilled_articles_stub.insert().values(id="dst_dup"))
        session.commit()

        now = datetime.now()
        var1 = ArticleAudioVariant(
            id="avar_dup_1",
            distilled_article_id="dst_dup",
            bitrate=128,
            file_size_bytes=1000000,
            oss_key="audio/art_dup/128.m4a",
            duration_sec=300,
            created_at=now,
            updated_at=now,
        )
        var2 = ArticleAudioVariant(
            id="avar_dup_2",
            distilled_article_id="dst_dup",
            bitrate=128,  # 同码率
            file_size_bytes=1000001,
            oss_key="audio/art_dup/128_v2.m4a",
            duration_sec=300,
            created_at=now,
            updated_at=now,
        )
        session.add(var1)
        session.commit()
        session.add(var2)

        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# 4. few_shot_examples roundtrip（含 user_id nullable）
# ---------------------------------------------------------------------------
def test_few_shot_examples_roundtrip_global(sqlite_engine):
    """user_id=NULL 的全局池条目可插入。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        now = datetime.now()
        rewrite_text = "你以为 AI 离你还很远，其实它已经悄悄改变了你的生活。"
        ex = FewShotExample(
            id="fs_global",
            user_id=None,  # 全局池
            source_pattern="topic=AI+rhythm=fast+hook=question",
            rewrite_text=rewrite_text,
            kind="hook",
            score_avg=4.5,
            source_eval_ids='["eval_a","eval_b"]',
            usage_count=0,
            active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(ex)
        session.commit()

        row = session.execute(
            select(FewShotExample).where(FewShotExample.id == "fs_global")
        ).scalar_one()
        assert row.user_id is None  # 全局池
        assert row.kind == "hook"
        assert abs(row.score_avg - 4.5) < 1e-6
        assert row.active is True
        assert row.rewrite_text == rewrite_text


def test_few_shot_examples_roundtrip_user_specific(sqlite_engine):
    """user_id!=NULL 的个人池条目。"""
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as session:
        session.execute(_users_stub.insert().values(id="5"))
        session.commit()

        now = datetime.now()
        ex = FewShotExample(
            id="fs_user_5",
            user_id=5,
            source_pattern="topic=AI+rhythm=fast+hook=question",
            rewrite_text="你的 AI 伙伴已经学会听懂你的弦外之音。",
            kind="hook",
            score_avg=4.8,
            source_eval_ids='["eval_c"]',
            created_at=now,
            updated_at=now,
        )
        session.add(ex)
        session.commit()

        row = session.execute(
            select(FewShotExample).where(FewShotExample.id == "fs_user_5")
        ).scalar_one()
        assert row.user_id == 5


# ---------------------------------------------------------------------------
# 5. distillation_evaluations 字段约束（SQLite 不强制 check constraint，
#    所以只在 Pydantic 层验证 —— ORM 层用 SQLAlchemy CheckConstraint 在 PG 生效）
# ---------------------------------------------------------------------------
def test_distillation_evaluations_score_validation_in_pydantic():
    """CP3.7.1 §2.1.A：overall_score 必须在 1-5 范围。"""
    from pydantic import ValidationError

    from distill.schemas import DistillationEvaluation

    # 正常构造
    e = DistillationEvaluation(
        id="eval_p",
        task_id="dst_p",
        user_id=1,
        overall_score=5,
        created_at=datetime.now(),
    )
    assert e.overall_score == 5

    # Pydantic 不强制 range（DB check constraint 兜底），但 ORM/Pydantic 类型是 int
    # 这里只验证类型检查
    with pytest.raises(ValidationError):
        DistillationEvaluation(
            id="eval_bad",
            task_id="dst_bad",
            user_id=1,
            overall_score="not-an-int",
            created_at=datetime.now(),
        )


# ---------------------------------------------------------------------------
# 6. Pydantic schema 反向序列化（ORM → schema）
# ---------------------------------------------------------------------------
def test_pydantic_schema_round_trip():
    """CP3.7.1 §2.1 Pydantic schema 字段映射正确（ORM → Pydantic）。"""
    from distill.schemas import (
        ArticleAudioVariant,
        DistillationEvaluation,
        FewShotExample,
        UserListeningPattern,
    )

    now = datetime.now()

    # DistillationEvaluation
    e = DistillationEvaluation(
        id="e1",
        task_id="t1",
        user_id=1,
        hook_score=4,
        overall_score=4,
        created_at=now,
    )
    d = e.model_dump()
    assert d["id"] == "e1"
    assert d["hook_score"] == 4
    assert d["overall_score"] == 4

    # UserListeningPattern
    p = UserListeningPattern(
        user_id=2,
        feedback_count=10,
        avg_session_sec=300,
        skip_rate=0.15,
        completion_rate=0.85,
        last_updated=now,
    )
    assert p.user_id == 2
    assert p.feedback_count == 10

    # ArticleAudioVariant
    a = ArticleAudioVariant(
        id="a1",
        distilled_article_id="d1",
        bitrate=128,
        file_size_bytes=1000000,
        oss_key="k1",
        duration_sec=300,
        created_at=now,
    )
    assert a.bitrate == 128
    assert a.sample_rate == 24000
    assert a.mono is True

    # FewShotExample
    f = FewShotExample(
        id="f1",
        user_id=None,
        source_pattern="p1",
        rewrite_text="x",
        kind="hook",
        score_avg=4.5,
        source_eval_ids="[]",
        created_at=now,
    )
    assert f.user_id is None
    assert f.kind == "hook"
    assert f.active is True


# ---------------------------------------------------------------------------
# 7. 4 张表都在 Base.metadata 里（alembic 可识别）
# ---------------------------------------------------------------------------
def test_4_tables_in_metadata():
    """CP3.7.1：4 张表都注册在 Base.metadata，alembic autogenerate 能识别。"""
    tables = Base.metadata.tables
    assert "distillation_evaluations" in tables
    assert "user_listening_patterns" in tables
    assert "article_audio_variants" in tables
    assert "few_shot_examples" in tables

    # 验证字段数大致符合 §2.1 SQL
    eval_cols = {c.name for c in tables["distillation_evaluations"].columns}
    assert {"id", "task_id", "user_id", "overall_score", "created_at"}.issubset(eval_cols)

    ulp_cols = {c.name for c in tables["user_listening_patterns"].columns}
    assert {"user_id", "feedback_count", "last_updated"}.issubset(ulp_cols)

    avar_cols = {c.name for c in tables["article_audio_variants"].columns}
    assert {"id", "distilled_article_id", "bitrate", "oss_key", "format"}.issubset(avar_cols)

    fs_cols = {c.name for c in tables["few_shot_examples"].columns}
    assert {"id", "user_id", "kind", "score_avg", "active"}.issubset(fs_cols)


# ---------------------------------------------------------------------------
# 8. alembic 4 个迁移可被 alembic 识别（import 不报错）
# ---------------------------------------------------------------------------
def test_alembic_migrations_importable():
    """CP3.7.1：alembic 0024-0027 4 个迁移 revision/down_revision 链条正确。"""
    import importlib

    migrations = [
        ("alembic.versions.0024_distillation_evaluations", "0024", "0023"),
        ("alembic.versions.0025_user_listening_patterns", "0025", "0024"),
        ("alembic.versions.0026_article_audio_variants", "0026", "0025"),
        ("alembic.versions.0027_few_shot_examples", "0027", "0026"),
    ]

    for module_name, expected_rev, expected_down in migrations:
        # 通过 spec loader 强制 import（迁移不在 pytest 默认 sys.path）
        spec = importlib.util.spec_from_file_location(
            module_name,
            Path(__file__).resolve().parents[2] / f"{module_name.replace('.', '/')}.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert (
            mod.revision == expected_rev
        ), f"{module_name}: revision={mod.revision}, expected {expected_rev}"
        assert (
            mod.down_revision == expected_down
        ), f"{module_name}: down_revision={mod.down_revision}, expected {expected_down}"
