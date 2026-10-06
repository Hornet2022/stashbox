"""2026-10 P0：三个列表端点在搬运 250KB×N 的 raw_content，而一个字节都不用。

## 问题

`articles.raw_content` 是抓取后的原始正文 JSONB —— `fetchers/parser.py` 把
`content_text` 限 50KB、`content_html` 限 200KB，单行可达 ~250KB。

三个列表端点都是 `select(Article, DistilledArticle)` 取整行 ORM 实体，
而 `_to_response` **一次都不读 `raw_content`**（只用 id / url / source /
title / user_id / status / favorite / skip / created_at / updated_at，
加 task 那侧的字段）。于是 250KB 从 PG 搬进 Python 进程再原样丢掉。

`/articles/pending` 与 `/articles/listened` 此前**连 LIMIT 都没有** ——
行数随用户历史线性增长，老用户一次请求就能把 content-service 内存打爆。

## 判据为什么是「看 SQL」而不是「看响应」

响应体本来就小（`_to_response` 不吐 raw_content），所以按响应断言永远是绿的 ——
缺陷发生在「DB → 进程」这一段，响应侧看不见。唯一能把它钉死的判据是
把端点真正发出的那条语句编译成 SQL，确认列清单里没有 raw_content。

这也是为什么这里要**调用真实的端点函数**而不是在测试里重写一遍查询：
重写一遍的话，测试通过与生产查询是否被 defer 完全无关。
"""

import pytest
from sqlalchemy.dialects import postgresql

from stashbox.backend.common.models import Article, DistilledArticle
from tests.content.helpers import content_main


# ---------------------------------------------------------------------------
# 假 DB：只记录端点真正发出去的语句
# ---------------------------------------------------------------------------


class _Rows:
    """execute() 的返回值：只提供端点用到的 all()。"""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None

    def one_or_none(self):
        return None


class RecordingSession:
    """记录 execute() 收到的语句，编译成 PG 方言 SQL 以便断言列清单。"""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Rows(self.rows)

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return None

    def sqls(self) -> list[str]:
        return [str(s.compile(dialect=postgresql.dialect())) for s in self.statements]

    async def commit(self):
        return None


def _make_rows(n: int = 1):
    """造 n 组 (Article, DistilledArticle) 瞬态对象（不入库）。"""
    rows = []
    for i in range(n):
        art = Article(
            id=f"art_{i}",
            user_id=7,
            url="https://x.com/a",
            source="web",
            title=f"标题{i}",
            status="pending",
            favorite=False,
            skip=False,
            raw_content={"content_text": "正" * 50000, "content_html": "<p>" + "x" * 200000},
        )
        task = DistilledArticle(
            id=f"dst_{i}",
            article_id=f"art_{i}",
            status="done",
            script_text="听感稿",
            duration_sec=60,
            tags=["科技"],
        )
        rows.append((art, task))
    return rows


@pytest.fixture
def pending_cache_off(monkeypatch):
    """绕开 pending 的 Redis 缓存，否则第一次调用就被缓存短路，测不到 SQL。"""
    from stashbox.backend.common import cache_service

    monkeypatch.setattr(cache_service, "get_pending", _async_return(None))
    monkeypatch.setattr(cache_service, "set_pending", _async_return(None))


def _async_return(value):
    async def _fn(*a, **k):
        return value

    return _fn


USER = {"sub": "7"}
CTX = {"job_id": "j", "redis": None}


# ---------------------------------------------------------------------------
# 1. 不再 select raw_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_articles_列表不拉_raw_content():
    db = RecordingSession(_make_rows(1))
    await content_main.list_articles(user=USER, db=db, limit=20, offset=0, tag=None)

    # list_articles 发两条语句：先 count(*) 拿 total，再取列表。所以按「全部语句」
    # 断言，而不是假定只有一条。
    sqls = db.sqls()
    assert sqls, "端点一条语句都没发，判据失效"
    assert not any(
        "raw_content" in s for s in sqls
    ), "列表 SELECT 里仍带 raw_content（单行可达 250KB）—— _to_response 又不读它"
    assert any("articles.id" in s for s in sqls), "判据失效：连 id 都没 select"


@pytest.mark.asyncio
async def test_pending_列表不拉_raw_content(pending_cache_off):
    db = RecordingSession(_make_rows(1))
    await content_main.list_pending(user=USER, db=db)

    (sql,) = db.sqls()
    assert "raw_content" not in sql


@pytest.mark.asyncio
async def test_listened_列表不拉_raw_content():
    db = RecordingSession(_make_rows(1))
    await content_main.list_listened(user=USER, db=db)

    (sql,) = db.sqls()
    assert "raw_content" not in sql


# ---------------------------------------------------------------------------
# 2. 无分页的两个端点必须有上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "kwargs"),
    [("list_pending", {}), ("list_listened", {})],
)
async def test_无分页端点_语句里带_LIMIT(endpoint, kwargs, pending_cache_off):
    db = RecordingSession(_make_rows(1))
    await getattr(content_main, endpoint)(user=USER, db=db, **kwargs)

    (sql,) = db.sqls()
    assert "LIMIT" in sql, f"{endpoint} 没有 LIMIT：行数随用户历史线性增长，能打爆内存"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list_pending", "list_listened"])
async def test_截断是_确定顺序_的(endpoint, pending_cache_off):
    """截断必须建立在 ORDER BY 之上，否则被丢掉的是随机行。

    没排序就 LIMIT N 的话，同一用户连续两次请求会拿到不同的集合 ——
    表现为「列表里那几篇偶尔会换」，极难复现也极难解释。
    """
    db = RecordingSession(_make_rows(1))
    await getattr(content_main, endpoint)(user=USER, db=db)

    (sql,) = db.sqls()
    assert "ORDER BY" in sql, f"{endpoint} 加了 LIMIT 却没有 ORDER BY，截断结果不确定"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list_pending", "list_listened"])
async def test_超过上限时_截断并告警(endpoint, pending_cache_off, caplog):
    """截断到上限，并且**留下可查的痕迹**。

    静默截断是最难查的一类问题：用户少了内容、日志干净、监控全绿。

    用 structlog 的 capture_logs 而不是 pytest 的 caplog：项目日志是 structlog
    接 JSON renderer 打到 stdout 的，caplog 抓不到（断言会永远红）。
    """
    from structlog.testing import capture_logs

    cap = content_main._LIST_PAGE_CAP
    db = RecordingSession(_make_rows(cap + 5))

    with capture_logs() as logs:
        result = await getattr(content_main, endpoint)(user=USER, db=db)

    assert result["count"] == cap, f"应截断到 {cap}，实际 {result['count']}"
    assert any(
        e.get("event") == "list_truncated" for e in logs
    ), "真的截断了却没有任何日志 —— 事后无法区分「截断」和「查询出错」"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list_pending", "list_listened"])
async def test_未超上限时_不告警(endpoint, pending_cache_off):
    """反向保障：正常用户（远不到上限）不该刷告警，否则这条日志很快就被无视。"""
    from structlog.testing import capture_logs

    db = RecordingSession(_make_rows(2))

    with capture_logs() as logs:
        result = await getattr(content_main, endpoint)(user=USER, db=db)

    assert result["count"] == 2
    assert not any(e.get("event") == "list_truncated" for e in logs)


# ---------------------------------------------------------------------------
# 3. 回归：截断不能改坏正常路径的响应
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_截断不改变_每条的响应形状(pending_cache_off):
    """加 LIMIT 和 defer 都是「少取数据」，必须证明没顺手改坏别的字段。

    尤其是 defer：一旦 _to_response 将来读了 raw_content，defer 过的端点会
    在访问那一刻触发 lazy load → async 上下文抛 MissingGreenlet（500）。
    """
    db = RecordingSession(_make_rows(3))

    result = await content_main.list_pending(user=USER, db=db)

    assert result["count"] == 3
    item = result["articles"][0]
    assert item["id"] == "art_0"
    assert item["title"] == "标题0"
    assert item["status"] == "ready", "task.status=done 应派生成 ready（_derive_status）"
    assert item["task_id"] == "dst_0"
    assert item["duration_sec"] == 60
    assert item["tags"] == ["科技"]
    # script_text 在列表走摘要分支（list_mode=True）
    assert item["script_text"] == "听感稿"
