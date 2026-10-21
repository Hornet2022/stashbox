"""陈旧任务兜底：卡死的蒸馏行不能把用户永久锁死。

## 背景

2026-10-21 内测实测：worker 崩溃 / Redis 丢消息 → `distilled_articles.status`
永远停在 `queued` → 幂等守卫（`IN_FLIGHT_DISTILL_STATUSES`）挡住**所有**重新
派发 → 用户既不能点「重试蒸馏」，也不能「换音色重新生成」。

而任务级重试（Arq）恰恰是这套系统对抗上游 vLLM 丢内容的**唯一**手段
（上游会偶发把生成内容整个丢掉，实测 12.5% 的概率 4 次尝试全空）。
一旦这条链断掉，整篇就彻底废了。

## 判据

状态还在 `queued`/`running`、但**多久没动过**超过 `STALE_DISTILL_TASK_SEC`
（默认 900s = 15 分钟）→ 判定为卡死，放行重新派发。

窗口取 15 分钟的依据（2026-10-21 实测）：单篇剪藏到就绪 2~4 分钟
（LLM 改写 ~11s + TTS 合成 2 段 ~100s + 落库），15 分钟有 4 倍余量，
不会误杀正常长任务。

## ⚠️ age 必须由 DB 算

`distilled_articles.updated_at` 是 `TIMESTAMP without time zone`（naive），
存的是**数据库会话时区**的墙上时间 —— 实测生产服务器是 **UTC+8**。
Python 进程若按 UTC 解释它，age 会算成**负数**，陈旧判定永远为假
（这个 bug 让兜底第一次上线时完全不生效 —— 实测触发返回 200 但队列为空）。

## ⚠️ 本测试不写库

测试连的 PostgreSQL 就是**生产库**（本机 5432），所以这里只用 mock
db 和只读查询，不 INSERT/UPDATE 任何行。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


@pytest.fixture(scope="module")
def ai_main():
    """加载 ai-service/main.py（目录名带连字符，按路径加载）。"""
    spec = importlib.util.spec_from_file_location("_stale_probe", AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_stale_probe"] = module
    spec.loader.exec_module(module)
    return module


class _FakeDA:
    def __init__(self, id: str = "dst_1", status: str = "running"):
        self.id = id
        self.status = status


class _FakeDB:
    """只回一个标量值（模拟 DB 算出的 age）。"""

    def __init__(self, value):
        self.value = value
        self.stmt = None

    async def scalar(self, stmt):
        self.stmt = stmt
        return self.value


async def test_stale_window_is_15_minutes(ai_main):
    """窗口 15 分钟 —— 实测单篇 2~4 分钟，4 倍余量。"""
    assert ai_main.STALE_DISTILL_TASK_SEC == 900


async def test_non_inflight_task_has_no_age(ai_main):
    """不是 in-flight 状态（done/failed）→ 不参与陈旧判定。"""
    db = _FakeDB(1200.0)
    assert await ai_main._stale_age_seconds(db, _FakeDA(status="done")) is None
    assert await ai_main._stale_age_seconds(db, _FakeDA(status="failed")) is None


async def test_no_task_has_no_age(ai_main):
    """没有行 → 不拦首次派发。"""
    assert await ai_main._stale_age_seconds(_FakeDB(0.0), None) is None


async def test_age_comes_from_db(ai_main):
    """age 由 DB 算（会话时区一致），不是 Python 自己减时间戳。"""
    db = _FakeDB(1200.5)
    age = await ai_main._stale_age_seconds(db, _FakeDA(status="queued"))
    assert age == 1200.5
    assert db.stmt is not None, "必须真的查库（而不是在 Python 里算）"


async def test_db_computed_age_is_positive(ai_main, db_session):
    """**只读**验证：DB 自己算出来的 age 是正数。

    这是本次 bug 的核心 —— Python 按 UTC 解释 DB 的 naive 时间戳会算出**负数**，
    陈旧判定永远为假。这里直接让 DB 算同一个表达式，确认它是正的。
    """
    from sqlalchemy import text

    age = await db_session.scalar(
        text(
            "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) "
            "FROM distilled_articles ORDER BY updated_at DESC LIMIT 1"
        )
    )
    if age is None:
        pytest.skip("库里没有 distilled_articles 行")
    assert float(age) > 0, f"DB 算的 age 应为正数（负数说明时区口径错了）：{age}"
