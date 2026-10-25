"""蒸馏任务里的**补图注**接线（2026-10-25）。

## 为什么蒸馏任务要管这件事

识图挪出剪藏请求后，文章是**带着占位符**落库的 ⇒ 补图注这一步被搬到了
蒸馏任务里（蒸馏之前，LLM 之前）。

## 这三条不变量，每一条都能安静地坏掉

1. **顺序**：必须**先补图注、再读正文**。
   反了就等于读到了还没补图注的旧正文 ⇒ 图注永远进不了稿子，
   而且表面上看一切正常（稿子能出来，只是没有图表信息）。
2. **兜底**：正文里**绝不能**留占位符。
   `\x00IMG3\x00` 进了 prompt，模型会把它当正文的一部分胡编。
3. **失败不炸**：补图注失败只意味着「这篇没有图表信息」，
   不能连累整篇蒸馏（那是"为了可有可无的图表，把整篇文章的稿子也赔进去"）。

⚠️ 全部不打真网络：`_load_raw_content` / `_run_distill_via_agent` / 补图注
三处都用替身顶掉。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from stashbox.backend.common import media_placeholders as placeholders
from tasks import distill_task as dt_module

CTX = {"job_id": "job-1", "redis": None}


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
    def __init__(self, recorder: list, *, article_exists: bool = True):
        self.recorder = recorder
        self.article_exists = article_exists

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.recorder.append(stmt)
        return _FakeResult()

    async def scalar(self, stmt):
        return "art_1" if self.article_exists else None

    async def commit(self):
        return None

    async def begin_nested(self):
        @asynccontextmanager
        async def _savepoint():
            yield self

        return _savepoint()


@pytest.fixture
def wired(monkeypatch):
    """把蒸馏任务的三处外部依赖打成替身，并记录调用顺序。"""
    order: list[str] = []
    agent_calls: list[dict] = []
    caption_calls: list[str] = []

    async def _fake_fill(article_id, **_kwargs):
        order.append("caption")
        caption_calls.append(article_id)
        return 2

    async def _load(db, article_id):
        order.append("load")
        # 正文里还带着占位符（补图注那一步会写回新的 raw_content，
        # 这里模拟"还没写回"的原始形态）
        return "第一段。\n\n" + placeholders.make_token(1) + "\n\n第二段。"

    async def _source(db, article_id):
        return "wechat_mp"

    async def _fake_run_via_agent(
        task_id, article_id, user_id, url, raw_content, source="web", simulate_failure=False
    ):
        agent_calls.append({"raw_content": raw_content})
        return {
            "status": "done",
            "current_step": "done",
            "rewritten_script": "改写稿",
            "tts_audio_url": "https://oss/a.mp3",
            "tts_duration_sec": 60,
        }

    async def _fake_persist(task_id, article_id, user_id, final):
        return None

    @asynccontextmanager
    async def _fake_session_local():
        yield _FakeSession([])

    monkeypatch.setattr(dt_module, "fill_article_captions", _fake_fill)
    monkeypatch.setattr(dt_module, "_load_raw_content", _load)
    monkeypatch.setattr(dt_module, "_load_material_source", _source)
    monkeypatch.setattr(dt_module, "_run_distill_via_agent", _fake_run_via_agent)
    monkeypatch.setattr(dt_module, "_persist_agent_final", _fake_persist)
    monkeypatch.setattr(dt_module, "AsyncSessionLocal", _fake_session_local)
    monkeypatch.setattr(dt_module, "tts_reload", _async_noop)
    monkeypatch.setattr(dt_module, "llm_reload", _async_noop_cfg)
    return {"order": order, "agent_calls": agent_calls, "caption_calls": caption_calls}


async def _async_noop():
    return None


async def _async_noop_cfg():
    return {"provider": "test", "model": "test"}


# ---------------------------------------------------------------------------
# 1. 顺序：先补图注，再读正文
# ---------------------------------------------------------------------------
async def test_先补图注再读正文(wired):
    await dt_module.distill_task(
        CTX, task_id="dst_1", article_id="art_1", user_id=1, url="https://x/y"
    )
    assert wired["order"] == ["caption", "load"], (
        "补图注必须在读正文之前 —— 反了就读不到写回的新正文，"
        "图注永远进不了稿子，而且表面上看一切正常"
    )
    assert wired["caption_calls"] == ["art_1"]


# ---------------------------------------------------------------------------
# 2. 兜底：正文里不留占位符
# ---------------------------------------------------------------------------
async def test_喂给llm的正文里没有占位符(wired):
    await dt_module.distill_task(
        CTX, task_id="dst_1", article_id="art_1", user_id=1, url="https://x/y"
    )
    assert wired["agent_calls"], "agent 应该被调用过"
    content = wired["agent_calls"][0]["raw_content"]
    assert "\x00" not in content, "占位符绝不能进 LLM 的 prompt"
    assert "IMG" not in content
    assert "第一段。" in content and "第二段。" in content, "正文本身必须完好"


async def test_落库态的占位符也要剥掉(wired, monkeypatch):
    """正文是从 JSONB 读出来的 ⇒ 可能是**落库态**（可见记法）。"""

    async def _load(db, article_id):
        return (
            "第一段。\n\n"
            + placeholders.to_storage_token(placeholders.make_token(2))
            + "\n\n第二段。"
        )

    monkeypatch.setattr(dt_module, "_load_raw_content", _load)

    await dt_module.distill_task(
        CTX, task_id="dst_1", article_id="art_1", user_id=1, url="https://x/y"
    )
    content = wired["agent_calls"][0]["raw_content"]
    assert "\x00" not in content
    assert "IMG" not in content
    assert "第一段。" in content


# ---------------------------------------------------------------------------
# 3. 失败不炸
# ---------------------------------------------------------------------------
async def test_补图注失败_蒸馏照跑(wired, monkeypatch):
    """补图注失败只意味着「这篇没有图表信息」。"""

    async def _boom(article_id, **_kwargs):
        raise RuntimeError("content-service 挂了")

    monkeypatch.setattr(dt_module, "fill_article_captions", _boom)

    result = await dt_module.distill_task(
        CTX, task_id="dst_1", article_id="art_1", user_id=1, url="https://x/y"
    )
    assert result.get("status") == "done", "补图注失败不该让整篇蒸馏失败"
    assert wired["agent_calls"], "正文照常喂给 agent"
    content = wired["agent_calls"][0]["raw_content"]
    assert "\x00" not in content, "兜底仍要保证正文干净"
