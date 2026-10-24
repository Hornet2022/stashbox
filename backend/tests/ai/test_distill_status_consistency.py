"""蒸馏状态口径统一 —— 两个端点必须给**同一个**答案。

## 背景

2026-10-25 实测：「这篇稿子现在什么状态」这个问题，仓里有**两份**实现：

- `content-service/main.py::_derive_status` —— 文章状态端点
  （`articles` LEFT JOIN `distilled_articles` 后派生）
- `ai-service/main.py::_external_distill_status` —— 任务状态端点
  （只看 `distilled_articles.status` 的内部 7 态 → 对外 5 态映射）

两份实现的**输入不同**（一份看两表，一份只看产物表）⇒ 同一个任务在两个端点上
给出**不同的答案**。三个具体后果（全部实测，不是推演）：

| 场景 | 任务端点（修前） | 文章端点（修前） |
|---|---|---|
| 终局失败（产物表停在 `queued`） | `pending` | `failed` |
| Arq 自动重试中间态（产物表 `failed`） | `failed` | `distilling` |
| 用户点了「重试蒸馏」（articles=`pending`） | `failed` | `pending` |

安卓 App 轮询的是**文章状态端点**（`GET /api/v1/articles/{id}/status`），
任务端点是另一处轮询 ⇒ 用户在两处看到不同的进度；且任务端点那个「永远
pending」看起来就是「蒸馏卡住了」。

⇒ 2026-10-25 收口到 `common/distill_status.py::derive_distill_status`，
两个端点调**同一个函数**。

## 这个文件锁住什么

1. **契约表**（纯函数）：产物表 status × articles.status → 对外 5 态
2. **同源**：两个服务用的是**同一个函数对象**；仓里**不再有**第二份映射表
3. **端点级一致**：同一组 DB 行，两个端点返回**同一个 status 字符串**
4. **写侧**：终局失败时产物表落 `failed` ——
   - 不落的话「重试蒸馏」会被入队幂等守卫挡住（守卫只放行 done/failed，
     产物表停在 `queued` 时要等 `STALE_DISTILL_TASK_SEC`=15 分钟的陈旧兜底
     才放行 ⇒ 用户点了重试，15 分钟里什么都没发生）
   - admin 统计（`failed_distillations_24h` / `distill_success_rate`）按
     `DistilledArticle.status='failed'` 算 ⇒ 不落就是系统性漏算失败率
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
CONTENT_DIR = BACKEND_DIR / "content-service"

from stashbox.backend.common.distill_status import (  # noqa: E402
    EXTERNAL_STATUSES,
    derive_distill_status,
    mark_distill_task_failed,
)
from stashbox.backend.common.models import DistilledArticle  # noqa: E402


def _load(name: str, path: Path):
    """按文件路径加载服务模块（目录名带连字符，不能当包导入）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def content_main():
    return _load("_distill_status_content_main", CONTENT_DIR / "main.py")


@pytest.fixture(scope="module")
def ai_main():
    return _load("_distill_status_ai_main", AI_DIR / "main.py")


class _FakeArticle:
    def __init__(self, status: str):
        self.status = status


class _FakeTask:
    def __init__(self, status: str | None):
        self.status = status


# ---------------------------------------------------------------------------
# A. 契约表（纯函数）
# ---------------------------------------------------------------------------

# (articles.status, 产物表 status, 期望对外状态, 为什么)
CONTRACT_CASES = [
    ("pending", "done", "ready", "产物成功 → ready（articles 可能还没跟上）"),
    ("distilling", "done", "ready", "产物成功 → ready"),
    ("listened", "done", "ready", "听完了 → 产物仍是 ready"),
    ("failed", "failed", "failed", "终局失败 → failed"),
    ("distilling", "failed", "distilling", "Arq 还会重试 → 不能显示 failed"),
    ("pending", "failed", "pending", "用户点了重试 → 不能显示 failed"),
    ("pending", "queued", "pending", "产物表还在排队 → 按 articles"),
    ("distilling", "queued", "distilling", "同上"),
    ("distilling", "step2_rewriting", "distilling", "内部中间态 → 按 articles"),
    ("pending", None, "pending", "没有产物行 → 按 articles"),
    ("distilling", None, "distilling", "同上"),
    ("failed", "queued", "failed", "产物表还没落 failed（老数据）→ 仍按 articles"),
    ("weird_status", "done", "ready", "articles 状态未知 + 产物成功 → ready"),
    ("weird_status", "queued", "distilling", "articles 状态未知 → 降级 distilling"),
]


@pytest.mark.parametrize(
    "article_status,task_status,expected,why",
    CONTRACT_CASES,
    ids=[f"{a}-{t}" for a, t, _e, _w in CONTRACT_CASES],
)
def test_derive_contract(article_status, task_status, expected, why):
    """纯函数的判定表 —— 口径说明见 common/distill_status.py 的 docstring。"""
    got = derive_distill_status(_FakeArticle(article_status), _FakeTask(task_status))
    assert got == expected, f"{why}：期望 {expected}，实得 {got}"


def test_result_always_in_external_enum():
    """对外状态**永远**在客户端强校验的枚举里（安卓遇到未知值会崩）。"""
    for article_status in ("pending", "distilling", "ready", "failed", "listened", "???", ""):
        for task_status in (
            "queued",
            "running",
            "step1_structuring",
            "done",
            "failed",
            "???",
            None,
        ):
            got = derive_distill_status(_FakeArticle(article_status), _FakeTask(task_status))
            assert got in EXTERNAL_STATUSES, f"{article_status}/{task_status} → {got}"


# ---------------------------------------------------------------------------
# B. 同源：两个服务用的是同一个函数，不是两份实现
# ---------------------------------------------------------------------------


def test_ai_service_uses_the_common_function(ai_main):
    """ai-service 的任务状态端点直接调 common 那份。"""
    assert ai_main.derive_distill_status is derive_distill_status


def test_ai_service_has_no_second_mapping(ai_main):
    """第二份映射表必须删干净 —— 留着一份就等于留着一个会漂的口径。"""
    assert not hasattr(ai_main, "_external_distill_status"), "还有第二份状态映射实现"
    assert not hasattr(ai_main, "_DISTILL_STATUS_MAP"), "还有第二份状态映射表"


def test_content_service_delegates_instead_of_copying(content_main):
    """content-service 的 `_derive_status` 必须**委托**给 common，不是抄一份。"""
    src = inspect.getsource(content_main._derive_status)
    assert "derive_distill_status(" in src, "必须在实现里调用 common 那份"
    # 抄一份的痕迹：自己维护状态集合 / 自己判 failed
    for smell in ("_DISTILL_STATUS_MAP", '"ready"', '"failed"', "IN_FLIGHT"):
        assert smell not in src, f"_derive_status 里出现了 {smell} —— 像是自己又判了一遍"


# ---------------------------------------------------------------------------
# C. 端点级一致（真 DB + 真端点函数）
# ---------------------------------------------------------------------------

# (articles.status, 产物表 status, 期望对外状态, 修之前两端口径差在哪)
ENDPOINT_CASES = [
    ("failed", "queued", "failed", "终局失败：任务端点给 pending、文章端点给 failed"),
    ("distilling", "failed", "distilling", "Arq 重试中：任务端点给 failed、文章端点给 distilling"),
    ("pending", "failed", "pending", "用户点了重试：任务端点给 failed、文章端点给 pending"),
    (
        "distilling",
        "queued",
        "distilling",
        "产物表还在排队：任务端点给 pending、文章端点给 distilling",
    ),
    ("failed", "failed", "failed", "两边都是 failed（本来就一致，锁住别改坏）"),
    ("pending", "done", "ready", "两边都是 ready"),
    ("listened", "done", "ready", "两边都是 ready"),
]


async def _mk_task(db_session, article_id: str, status: str) -> DistilledArticle:
    task = DistilledArticle(
        id=f"dst_{uuid.uuid4().hex[:24]}",
        article_id=article_id,
        status=status,
        audio_url="https://oss.example.com/a.mp3",
        duration_sec=120,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.parametrize(
    "article_status,task_status,expected,note",
    ENDPOINT_CASES,
    ids=[f"{a}-{t}" for a, t, _e, _n in ENDPOINT_CASES],
)
async def test_two_endpoints_return_the_same_status(
    db_session, article_factory, content_main, ai_main, article_status, task_status, expected, note
):
    """**同一个任务**，两个端点必须给同一个 status 字符串。

    这是用户真正能看见的那条契约：App 在两个地方轮询状态，
    两处不一致 = 用户看到「一处在转、一处卡住」。
    """
    art = await article_factory(status=article_status)
    task = await _mk_task(db_session, art.id, task_status)
    user = {"sub": str(art.user_id)}

    content_view = await content_main.article_status(art.id, user, db_session)
    ai_view = await ai_main.distill_status(task.id, user, db_session)

    assert content_view.status == ai_view["status"], (
        f"两个端点口径不一致（{note}）：文章端点 {content_view.status} / "
        f"任务端点 {ai_view['status']}"
    )
    assert content_view.status == expected, note


# ---------------------------------------------------------------------------
# D. 写侧：终局失败必须落产物表
# ---------------------------------------------------------------------------


async def test_terminal_failure_marks_product_row_failed(db_session, article_factory):
    """终局失败 → 产物表落 `failed`（原来只有 agent 路径会落）。"""
    art = await article_factory(status="failed")
    task = await _mk_task(db_session, art.id, "running")

    await mark_distill_task_failed(db_session, art.id)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.status == "failed"
    # 终局失败 = 本次没有产出 ⇒ 行上不该留着上一轮的音频地址
    # （`status='failed'` 却挂着可播 URL 是自相矛盾；mock 路径早就这么收口了）
    assert task.audio_url is None
    assert task.duration_sec is None


async def test_terminal_failure_does_not_downgrade_a_success(db_session, article_factory):
    """已经 `done` 的产物行不能被后一次失败打成 `failed`。

    重跑时 `_requeue_or_create` 会把产物行置回 `queued`（**故意不清**旧产物字段），
    但若这一行已经是 `done`，说明有一轮**成功**过 —— 不能被覆盖。
    """
    art = await article_factory(status="distilling")
    task = await _mk_task(db_session, art.id, "done")

    await mark_distill_task_failed(db_session, art.id)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.status == "done"
    assert task.audio_url == "https://oss.example.com/a.mp3"


async def test_failed_row_does_not_block_user_retry(db_session, article_factory, ai_main):
    """产物表落 `failed` 之后，用户点「重试蒸馏」**不会被幂等守卫挡住**。

    入队守卫只放行产物表状态为 done/failed 的行（`IN_FLIGHT_DISTILL_STATUSES`
    = queued/running 才是「还在跑」）。产物表停在 `queued` 时，
    用户点了重试要等到 `STALE_DISTILL_TASK_SEC`（15 分钟）的陈旧兜底才放行
    ⇒ 按钮按下去 15 分钟没反应。
    """
    art = await article_factory(status="failed")
    task = await _mk_task(db_session, art.id, "queued")

    await mark_distill_task_failed(db_session, art.id)
    await db_session.commit()
    await db_session.refresh(task)

    assert (
        task.status not in ai_main.IN_FLIGHT_DISTILL_STATUSES
    ), f"产物表状态 {task.status} 会被幂等守卫挡住 ⇒ 用户点重试没反应"


# ---------------------------------------------------------------------------
# E. 接线：distill_task 的终局失败分支真的写了产物表
# ---------------------------------------------------------------------------


async def test_distill_task_terminal_failure_writes_product_row(
    monkeypatch, db_session, article_factory
):
    """跑一次真的 distill_task（agent 抛异常 + 最后一次尝试）→ 产物表落 failed。

    这一条是**接线**断言：前面 D 组验的是 helper 本身，这里验的是
    「终局失败分支真的调了它」—— helper 写对了但没接上，等于没修。
    """
    from tasks import distill_task as dt_module

    art = await article_factory(
        raw_content={
            "content_text": "这是正文。" * 40,
            "title": "测试文章",
            "media_urls": [],
            "source": "wechat_mp",
        },
        status="distilling",
    )
    task = await _mk_task(db_session, art.id, "queued")
    await db_session.commit()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("agent boom")

    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _boom)

    # job_try=3 = 最后一次尝试（max_tries = retry_max + 1 = 3）⇒ 终局失败。
    # quota_charged=False ⇒ 不退配额（不碰配额状态）。
    with pytest.raises(RuntimeError, match="agent boom"):
        await dt_module.distill_task(
            {"job_id": "j", "redis": None, "job_try": 3},
            task.id,
            art.id,
            art.user_id,
            art.url,
            title=art.title,
            quota_charged=False,
        )

    await db_session.refresh(task)
    await db_session.refresh(art)
    assert art.status == "failed", "articles 侧要落终局失败"
    assert task.status == "failed", "产物表也要落终局失败（否则重试被守卫挡 15 分钟）"


async def test_distill_task_intermediate_retry_does_not_mark_product_row_failed(
    monkeypatch, db_session, article_factory
):
    """中间态（Arq 还会重试）→ 产物表**不能**落 failed。

    中间态落 failed ⇒ 用户提前看到「失败 / 换个源试试？」→ 点「重试蒸馏」
    → 白烧一次 LLM + TTS（内测实测：首轮失败、15 秒后 Arq 重试**成功**）。
    """
    from tasks import distill_task as dt_module

    art = await article_factory(
        raw_content={
            "content_text": "这是正文。" * 40,
            "title": "测试文章",
            "media_urls": [],
            "source": "wechat_mp",
        },
        status="distilling",
    )
    task = await _mk_task(db_session, art.id, "queued")
    await db_session.commit()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("agent boom")

    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _boom)

    # job_try=1 ⇒ 还有下一次尝试（will_retry=True）
    with pytest.raises(RuntimeError, match="agent boom"):
        await dt_module.distill_task(
            {"job_id": "j", "redis": None, "job_try": 1},
            task.id,
            art.id,
            art.user_id,
            art.url,
            title=art.title,
            quota_charged=False,
        )

    await db_session.refresh(task)
    await db_session.refresh(art)
    assert art.status == "distilling", "中间态要保持「还在进行」"
    assert task.status != "failed", "中间态不能把产物表打成 failed（会让用户提前看到失败页）"


async def test_empty_content_path_also_marks_product_row_failed(
    monkeypatch, db_session, article_factory
):
    """正文为空的早退路径（终局失败，不重试）→ 产物表也要落 failed。

    这条路径此前**只**写 `articles.status='failed'`，产物表永远停在 `queued`
    ⇒ ①用户点重试被守卫挡 15 分钟 ②admin 失败统计一次都算不到。
    """
    from tasks import distill_task as dt_module

    art = await article_factory(raw_content=None, status="distilling")
    task = await _mk_task(db_session, art.id, "queued")
    await db_session.commit()

    result = await dt_module.distill_task(
        {"job_id": "j", "redis": None, "job_try": 1},
        task.id,
        art.id,
        art.user_id,
        art.url,
        title=art.title,
        quota_charged=False,
    )
    assert result["status"] == "failed"

    await db_session.refresh(task)
    await db_session.refresh(art)
    assert art.status == "failed"
    assert task.status == "failed", "早退失败路径也要落产物表"


# ---------------------------------------------------------------------------
# F. 回归护栏：老用例里的旧契约不能被改回来
# ---------------------------------------------------------------------------


async def test_status_endpoint_and_task_endpoint_agree_for_done(db_session, article_factory):
    """成功路径：两个端点都给 `ready`（别把成功路径改坏）。"""
    art = await article_factory(status="ready")
    task = await _mk_task(db_session, art.id, "done")
    rows = (
        await db_session.execute(select(DistilledArticle).where(DistilledArticle.id == task.id))
    ).scalar_one()
    assert rows.status == "done"
    assert derive_distill_status(art, rows) == "ready"
