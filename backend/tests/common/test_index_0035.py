"""0035 四个索引：模型 / 迁移 / 真实库 三方一致（2026-10）。

## 为什么索引名要专门测

索引名在三个地方各写一遍：`common/models/*.py` 的 `__table_args__`、
`alembic/versions/0035_listing_indexes.py`、以及 PG 里的真实索引名。
**任一处不同名**，后果都不是报错而是静默漂移：

  - 模型叫 A、迁移建 B → `alembic check` 认为「索引 A 被删了、B 是多余的」，
    下一个人跑 autogenerate 就生成一条把它「改回来」的迁移，纯噪音；
  - 迁移建了、模型没写 → 同上，反方向；
  - 三处都写了但列顺序不同 → 不报错，只是索引没起到预期作用（最难查）。

所以这里直接把三个来源拉到一起比对，而不是逐个断言「索引存在」。

另外顺带钉住**每个索引服务于哪个查询**——纯断言「索引存在」挡不住
「索引建了但查询根本不匹配」，那是最常见也最冤的一种优化。
"""

import importlib.util
from pathlib import Path

import pytest

from stashbox.backend.common.models import Article, DistilledArticle, DistillationEvaluation

BACKEND_DIR = Path(__file__).resolve().parents[2]

# 迁移里声明的四个索引：名字 → (表, 列)
EXPECTED = {
    "idx_articles_user_created": ("articles", ["user_id", "created_at DESC"]),
    "idx_distilled_status_updated": ("distilled_articles", ["status", "updated_at"]),
    "idx_eval_created": ("distillation_evaluations", ["created_at"]),
    "idx_distilled_tags_gin": ("distilled_articles", ["tags"]),
}

MODELS = {
    "articles": Article.__table__,
    "distilled_articles": DistilledArticle.__table__,
    "distillation_evaluations": DistillationEvaluation.__table__,
}


def _load_migration():
    path = BACKEND_DIR / "alembic" / "versions" / "0035_listing_indexes.py"
    spec = importlib.util.spec_from_file_location("alembic.versions.0035", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_index(table_name: str, index_name: str):
    return {ix.name: ix for ix in MODELS[table_name].indexes}.get(index_name)


def _norm(expr, table_name: str) -> str:
    """把模型里的列表达式归一成裸列名。

    `Index("i", "user_id")` 在 metadata 里解析成的是 `articles.user_id`
    （带表名），而 `text("created_at DESC")` 保持原样。直接比字符串会
    因为前缀差异假红，得先剥掉 —— 剥的方式要保留 DESC 这类修饰词。
    """
    s = expr.text if hasattr(expr, "text") else str(expr)
    prefix = f"{table_name}."
    return s[len(prefix) :] if s.startswith(prefix) else s


# ---------------------------------------------------------------------------
# 1. 迁移自身：revision 链 + 声明内容
# ---------------------------------------------------------------------------


def test_迁移_revision_链正确():
    mod = _load_migration()

    assert mod.revision == "0035"
    assert mod.down_revision == "0034", "0035 必须接在 0034 之后，否则 alembic 会分叉"


def test_迁移声明的索引_与本文件期望一致():
    """迁移里的 _SPECS 与本文件的 EXPECTED 必须一致。

    改了一边忘了另一边的话，两个文件互相印证不了任何东西 —— 所以两边
    都对着同一份「事实」核。
    """
    mod = _load_migration()

    declared = {name: (table, [str(c) for c in cols]) for name, table, cols, _gin in mod._SPECS}

    assert set(declared) == set(EXPECTED), (
        f"迁移里的索引与期望不符：多 {set(declared) - set(EXPECTED)}、"
        f"少 {set(EXPECTED) - set(declared)}"
    )
    for name, (table, cols) in EXPECTED.items():
        assert declared[name][0] == table, f"{name} 挂错表了：{declared[name][0]} != {table}"
        assert declared[name][1] == cols, f"{name} 的列不对：{declared[name][1]} != {cols}"


# ---------------------------------------------------------------------------
# 2. 模型侧同名同列
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index_name", sorted(EXPECTED))
def test_模型里声明了同名索引(index_name):
    table_name, cols = EXPECTED[index_name]

    ix = _model_index(table_name, index_name)
    assert ix is not None, (
        f"{table_name}.{index_name} 没写进模型的 __table_args__ —— "
        "autogenerate 会认为它被删了，下次跑就生成一条把它建回去的迁移"
    )

    got = [_norm(c, table_name) for c in ix.expressions]
    assert got == cols, f"{index_name} 列顺序/内容不对：{got} != {cols}"


def test_gin_索引用的是_gin_不是_btree():
    """tags 是 JSONB，GIN 才能加速 `@>` 包含查询。

    写成默认 btree 的话：建索引不报错、`@>` 也照样跑（只是全表扫），
    属于「优化做了但没生效」且没有任何提示 —— 所以单独钉住。
    """
    ix = _model_index("distilled_articles", "idx_distilled_tags_gin")

    assert (
        ix.dialect_options["postgresql"]["using"] == "gin"
    ), "tags 是 JSONB 且查询用的是 @> 包含运算符，必须 GIN；btree 会静默退化成全表扫"


# ---------------------------------------------------------------------------
# 3. 真实库里确实存在（需要本机 PG 已 migrate 到 head）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_真实库里_四个索引都在():
    from sqlalchemy import text

    from stashbox.backend.common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text("select indexname from pg_indexes " "where indexname = ANY(:names)"),
                {"names": list(EXPECTED)},
            )
        ).all()
    found = {r[0] for r in rows}

    missing = set(EXPECTED) - found
    assert not missing, (
        f"库里缺这些索引：{missing} —— 本机没跑 alembic upgrade head？" "迁移写了不等于库里有"
    )


@pytest.mark.asyncio
async def test_真实库里_tags_索引_确实是_gin():
    from sqlalchemy import text

    from stashbox.backend.common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ddl = (
            await db.execute(
                text("select indexdef from pg_indexes where indexname = 'idx_distilled_tags_gin'")
            )
        ).scalar()

    assert ddl is not None, "索引不存在，先跑 alembic upgrade head"
    assert "USING gin" in ddl, f"实际建的不是 GIN：{ddl}"


# ---------------------------------------------------------------------------
# 4. 索引必须真的服务于某个查询形状（防止「建了但没用」）
# ---------------------------------------------------------------------------


def test_articles_复合索引_与列表端点的查询形状对齐():
    """列表端点：WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC

    复合索引的列序必须是 (user_id, created_at DESC)：等值列在前、排序列在后。
    反过来 (created_at, user_id) 也能用，但要先扫大量行再过滤 user_id ——
    用户数一多就是灾难。方向反了不算错，但等于白建。
    """
    ix = _model_index("articles", "idx_articles_user_created")
    assert ix is not None
    cols = [_norm(c, "articles") for c in ix.expressions]

    assert cols == [
        "user_id",
        "created_at DESC",
    ], f"列序错了：{cols}。等值列在前、排序列在后才吃得上索引"


@pytest.mark.asyncio
async def test_列表端点_确实按_created_at_倒序取数():
    """反向保障：索引带 DESC，上层的查询也必须真的 ORDER BY DESC。

    只测索引定义会漏掉「查询没排序」这种不匹配的组合 —— 那样索引只被
    当成查找用的普通索引，一点排序收益都拿不到。
    """
    from tests.content.helpers import content_main
    from tests.content.test_list_endpoints_query_shape import USER, RecordingSession, _make_rows

    db = RecordingSession(_make_rows(1))
    await content_main.list_listened(user=USER, db=db)

    assert any(
        "ORDER BY articles.created_at DESC" in s for s in db.sqls()
    ), "列表端点没有按 created_at DESC 排序 —— idx_articles_user_created 的排序收益拿不到"
