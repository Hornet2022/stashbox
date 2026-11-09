"""蒸馏失败的**用户可见原因** + **失败率统计不再虚高**（2026-11-09 内测事故）。

## 事故（生产实测数字）

上游 `tertu.cn` 整机不可达，4 篇内测文章的蒸馏全挂在 rewrite 步骤。

1. **用户看到的失败原因是一串英文黑话**：
   `articles.error` 写的是
   `"agent distill failed at rewrite (timeout): openai chat failed after 3 attempts: TimeoutError()"`
   —— 用户既不知道为什么失败，也不知道该不该马上重试。

2. **失败率统计虚高 6 倍**：`feedback` 表里 `distill_failed` 记了 **24 条**，
   而真实失败只有 **4 篇**。
   根因：`_persist_agent_final` 在**每次尝试**末尾都会跑（agent 返回失败也走到），
   而 Arq 重试 5 次 ⇒ 一次真实失败记 5~6 条。
   终局失败的埋点本来有一处守卫（"只在最后一次尝试才记"），但被这条旁路绕过了。

## 本文件钉住的行为

- 失败时 `articles.error` 写**面向用户的中文文案**，且带机器可读的 kind。
- 终局失败**恰好记 1 条** `distill_failed`；中间尝试**一条都不记**。
- `_persist_agent_final` 里**不许**再出现 `DISTILL_FAILED` 埋点（静态钉住）。

**护栏价值**：把用户文案改回技术串、或把 `_persist_agent_final` 的埋点加回来，
本文件立刻变红。
"""

import inspect
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from stashbox.backend.common.distill_failure_reason import (  # noqa: E402
    DEFAULT_MESSAGE,
    user_failure_message,
)
from tasks import distill_task as dt_module  # noqa: E402


# ── 用户文案 ────────────────────────────────────────────────────────────


def test_user_message_is_chinese_and_actionable():
    """面向用户的文案要说人话 + 给下一步，不能是英文技术串。"""
    msg = user_failure_message("upstream_unreachable")
    assert "AI" in msg
    assert "重试" in msg
    for token in ("upstream", "TimeoutError", "agent distill failed", "Traceback"):
        assert token not in msg, f"用户文案里混进了技术串：{token}"


def test_unknown_kind_falls_back_to_generic_message():
    """认不出的 kind 也要给一句人话，**不能返回空串**。

    空串 ⇒ 客户端显示成"没有原因"，比一句笼统的话更糟。
    """
    assert user_failure_message(None)
    assert user_failure_message("some-brand-new-kind") == DEFAULT_MESSAGE


@pytest.mark.parametrize(
    "kind",
    ["upstream_unreachable", "upstream_unavailable", "timeout", "ratelimit", "auth", "internal"],
)
def test_every_known_kind_has_a_message(kind):
    assert user_failure_message(kind) != DEFAULT_MESSAGE


# ── 失败率统计 ──────────────────────────────────────────────────────────


def test_persist_agent_final_must_not_track_distill_failed():
    """🔴 静态护栏：`_persist_agent_final` 里**不许**再埋 DISTILL_FAILED。

    它每次尝试都会跑 ⇒ 在这里埋点 = 失败次数 × 重试次数 = 统计虚高 6 倍。
    终局失败的埋点归 `distill_task` 的失败处理器（只在 will_retry=False 时记）。
    """
    src = inspect.getsource(dt_module._persist_agent_final)
    # 只看**代码**（EventName.DISTILL_FAILED），不看 docstring 里提到它的散文 ——
    # 散文提到是正常的，代码里再埋一次才是病。
    assert "EventName.DISTILL_FAILED" not in src, (
        "_persist_agent_final 又开始记 DISTILL_FAILED 了 ⇒ "
        "每次重试都会多记一条，失败率统计会再次虚高"
    )


# ── 端到端：失败路径的行为 ─────────────────────────────────────────────


class _FakeResult:
    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _FakeSession:
    """记录 execute() 收到的语句；不做真实 DB IO。"""

    def __init__(self, recorder: list):
        self.recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.recorder.append(stmt)
        return _FakeResult()

    async def scalar(self, stmt):
        return "art_1"

    async def commit(self):
        return None

    async def begin_nested(self):
        @asynccontextmanager
        async def _savepoint():
            yield self

        return _savepoint()


@pytest.fixture
def failed_run(monkeypatch):
    """跑一遍「agent 失败」路径，返回 (记录的埋点, 记录的 Article update)。"""
    statements: list = []
    events: list[str] = []

    @asynccontextmanager
    async def _fake_session_local():
        yield _FakeSession(statements)

    async def _fake_track(db, event, *args, **kwargs):
        events.append(str(getattr(event, "value", event)))

    async def _fake_track_simple(db, event, *args, **kwargs):
        events.append(str(getattr(event, "value", event)))

    async def _fake_agent(*args, **kwargs):
        return {
            "status": "failed",
            "error_kind": "upstream_unreachable",
            "error_step": "rewrite",
            "error": "openai chat failed after 3 attempts: [unreachable] ConnectError()",
        }

    async def _noop(*args, **kwargs):
        return None

    async def _raw_content(db, article_id):
        return "正文"

    async def _source(db, article_id):
        return "wechat_mp"

    async def _title(db, article_id):
        return "标题"

    async def _reload():
        return {}

    monkeypatch.setattr(dt_module, "AsyncSessionLocal", _fake_session_local)
    monkeypatch.setattr(dt_module, "track", _fake_track)
    monkeypatch.setattr(dt_module, "track_simple", _fake_track_simple)
    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _fake_agent)
    monkeypatch.setattr(dt_module, "_persist_agent_final", _noop)
    monkeypatch.setattr(dt_module, "_load_raw_content", _raw_content)
    monkeypatch.setattr(dt_module, "_load_material_source", _source)
    monkeypatch.setattr(dt_module, "_load_article_title", _title)
    monkeypatch.setattr(dt_module, "fill_article_captions", _noop)
    monkeypatch.setattr(dt_module, "llm_reload", _reload)
    monkeypatch.setattr(dt_module, "mark_distill_task_failed", _noop)
    monkeypatch.setattr(dt_module, "_refund_quota_once", _noop)
    monkeypatch.setattr(dt_module, "load_arq_config", lambda: SimpleNamespace(retry_max=2))

    return statements, events


def _article_error_values(statements) -> list:
    """从记录的 update 语句里取出 Article.error 的值。"""
    out = []
    for stmt in statements:
        for column, value in getattr(stmt, "_values", {}).items():
            if column.key == "error":
                out.append(value.value if hasattr(value, "value") else value)
    return out


@pytest.mark.asyncio
async def test_terminal_failure_writes_user_facing_chinese_reason(failed_run):
    """🔴 护栏：`articles.error` 必须是**给用户看的中文**，不是英文技术串。"""
    statements, _events = failed_run

    ctx = {"job_id": "job-1", "redis": None, "job_try": 3}  # 最后一次尝试
    with pytest.raises(Exception):
        await dt_module.distill_task(ctx, "dst_1", "art_1", 7, "https://x.com/a")

    errors = [e for e in _article_error_values(statements) if e]
    assert errors, "失败路径没有写 articles.error ⇒ 用户看不到任何原因"
    for value in errors:
        assert "AI" in value and "重试" in value
        assert "agent distill failed" not in value
        assert "TimeoutError" not in value


@pytest.mark.asyncio
async def test_terminal_failure_tracks_exactly_one_distill_failed(failed_run):
    """🔴 护栏：终局失败**恰好一条** distill_failed（不能是重试次数那么多条）。"""
    _statements, events = failed_run

    ctx = {"job_id": "job-1", "redis": None, "job_try": 3}  # 最后一次尝试
    with pytest.raises(Exception):
        await dt_module.distill_task(ctx, "dst_1", "art_1", 7, "https://x.com/a")

    failed_events = [e for e in events if "distill_failed" in e]
    assert len(failed_events) == 1, f"终局失败应只记 1 条，实际 {len(failed_events)} 条"


@pytest.mark.asyncio
async def test_intermediate_attempt_tracks_no_distill_failed(failed_run):
    """🔴 护栏：中间尝试（还会重试）**不记** distill_failed。

    否则"这篇最终成功"的稿子也会留下失败记录 —— 失败率统计直接失真。
    """
    _statements, events = failed_run

    ctx = {"job_id": "job-1", "redis": None, "job_try": 1}  # 还会重试
    with pytest.raises(Exception):
        await dt_module.distill_task(ctx, "dst_1", "art_1", 7, "https://x.com/a")

    assert not [e for e in events if "distill_failed" in e]


@pytest.mark.asyncio
async def test_success_clears_stale_error(monkeypatch):
    """🔴 护栏：**成功时必须清掉 articles.error**。

    实测（2026-11-09）：文章失败过 → error 写着「AI 服务这次响应超时了，
    请稍后重试。」；后来重跑成功 → status 变 ready，但 error **留在库里** ⇒
    App 读到「已就绪 + 一句失败提示」，用户以为还没成功。

    error 的语义是「**当前**为什么失败」；一旦成功就没有失败原因了。
    """
    from stashbox.backend.common.models import Article as ArticleModel
    from stashbox.backend.common.models.distilled_article import DistilledArticle as DAModel

    captured: dict = {}

    class _OkResult:
        """按调用顺序返回产物行 / 文章行。"""

        def __init__(self, *rows):
            self._rows = list(rows)

        def scalar_one_or_none(self):
            return self._rows.pop(0) if self._rows else None

        def scalars(self):
            return self

        def all(self):
            return []

    class _OkSession:
        def __init__(self, recorder: list):
            self.recorder = recorder
            self._queue = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt):
            self.recorder.append(stmt)
            if self._queue:
                return _OkResult(self._queue.pop(0))
            return _OkResult()

        async def scalar(self, stmt):
            return "art_1"

        async def commit(self):
            return None

        def begin_nested(self):
            @asynccontextmanager
            async def _savepoint():
                yield self

            return _savepoint()

    da = DAModel(id="dst_1", article_id="art_1", status="done", audio_url="https://cdn/a.mp3")
    art = ArticleModel(id="art_1", user_id=7, status="distilling")
    art.error = "AI 服务这次响应超时了，请稍后重试。"  # 上一次失败的残留

    factory_sessions = []

    @asynccontextmanager
    async def _fake_session_local():
        s = _OkSession([])
        s._queue.extend([da, art])
        factory_sessions.append(s)
        yield s

    async def _fake_agent(*args, **kwargs):
        return {
            "status": "done",
            "rewritten_script": "改写稿",
            "tts_audio_url": "https://cdn/a.mp3",
            "tts_duration_sec": 60,
        }

    async def _noop(*args, **kwargs):
        return None

    async def _raw_content(db, article_id):
        return "正文"

    async def _source(db, article_id):
        return "wechat_mp"

    async def _title(db, article_id):
        return "标题"

    async def _reload():
        return {}

    monkeypatch.setattr(dt_module, "AsyncSessionLocal", _fake_session_local)
    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _fake_agent)
    monkeypatch.setattr(dt_module, "_persist_agent_final", _noop)
    monkeypatch.setattr(dt_module, "_load_raw_content", _raw_content)
    monkeypatch.setattr(dt_module, "_load_material_source", _source)
    monkeypatch.setattr(dt_module, "_load_article_title", _title)
    monkeypatch.setattr(dt_module, "fill_article_captions", _noop)
    monkeypatch.setattr(dt_module, "llm_reload", _reload)
    monkeypatch.setattr(dt_module, "mark_distill_task_failed", _noop)
    monkeypatch.setattr(dt_module, "_refund_quota_once", _noop)
    monkeypatch.setattr(dt_module, "load_arq_config", lambda: SimpleNamespace(retry_max=2))

    result = await dt_module.distill_task(
        {"job_id": "job-1", "redis": None}, "dst_1", "art_1", 7, "https://x.com/a"
    )
    assert result["status"] == "done"

    captured["art"] = art
    assert art.status == "ready"
    assert art.error is None, "成功了却留着上一轮的失败文案 ⇒ 用户看到「已就绪 + 失败提示」"
