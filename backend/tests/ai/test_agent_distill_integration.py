"""CP-AGENT-RUNNER-INTEGRATION：distill_task.py 通过 agent_app 跑蒸馏（生产路径接入）。

覆盖：
  - _run_distill_via_agent 把 DistillContext-like 输入 → AgentState → agent_app.ainvoke
  - 成功后 _persist_agent_final 把 final state 写回 distilled_articles
  - 失败后 error_kind / error_step 写到 distilled_articles.metadata

注意：monkeypatch MemoryStore + agent_app，避免依赖真 DB + 真 LLM。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_AI_SERVICE))


@pytest.fixture(autouse=True)
def reset_default_registry_singleton():
    from agent import tools as tools_module

    tools_module._default_registry_singleton = None
    yield
    tools_module._default_registry_singleton = None


@pytest.fixture
def patch_agent_runner_deps(monkeypatch):
    """monkeypatch _run_distill_via_agent 用到的依赖：
    - MemoryStore.load_user_profile → 返回固定 profile
    - MemoryStore.load_few_shots → 返回固定 few-shot 列表
    - agent_app.ainvoke → mock 跑完直接返回 final state
    - _persist_agent_final 的 DB 写入 → 走 None path（不连 DB）
    """
    from agent import memory as memory_module
    from agent import runner as runner_module
    from tasks import distill_task

    # 1. monkeypatch MemoryStore
    class _FakeProfile:
        def __init__(self):
            self.user_id = 999
            self.tier = "pro"
            self.ab_group = "personalized"
            self.preferences = {"length_pref": "短"}

    async def fake_load_profile(user_id):
        return _FakeProfile()

    class _FakeFewShot:
        def __init__(self):
            self.input_excerpt = "示例新闻"
            self.output_excerpt = "各位听众..."
            self.score = 9.0
            self.tags = ("科技",)

    async def fake_load_few_shots(topic=None, limit=3):
        return [_FakeFewShot()]

    class _FakeMemoryStore:
        def __init__(self, session_factory=None):
            self._session_factory = session_factory

        async def load_user_profile(self, user_id):
            return await fake_load_profile(user_id)

        async def load_few_shots(self, topic=None, limit=3):
            return await fake_load_few_shots(topic, limit)

    monkeypatch.setattr(memory_module, "MemoryStore", _FakeMemoryStore)

    # 2. monkeypatch agent_app.ainvoke
    #    第一次 ainvoke 返回 final state（模拟成功）
    #    ainvoke 的 state 通过 stdin，但最终 final state 是 dict
    final_state = {
        "article_id": "art_test",
        "user_id": 999,
        "url": "https://x",
        "source": "web",
        "status": "done",
        "current_step": "done",
        "rewritten_script": "改写后的稿子",
        "tts_audio_url": "https://oss.example.com/audio.mp3",
        "tts_duration_sec": 60,
        "final_audio_url": "https://oss.example.com/audio.mp3",
        "final_duration_sec": 60,
    }

    async def fake_ainvoke(state):
        return final_state

    monkeypatch.setattr(runner_module.agent_app, "ainvoke", fake_ainvoke)

    # 3. monkeypatch _persist_agent_final 跳过 DB
    async def fake_persist(task_id, article_id, user_id, final):
        # 简单验证 final state 字段类型
        return None

    monkeypatch.setattr(distill_task, "_persist_agent_final", fake_persist)

    return runner_module, memory_module, distill_task


@pytest.mark.asyncio
async def test_run_distill_via_agent_success(patch_agent_runner_deps):
    """CP-AGENT-RUNNER-RUN-SUCCESS：成功路径调 agent_app.ainvoke + _persist_agent_final。"""
    from tasks import distill_task

    final = await distill_task._run_distill_via_agent(
        task_id="dst_test_001",
        article_id="art_test_001",
        user_id=999,
        url="https://example.com",
        raw_content="原文内容：今天苹果发布了 iPhone。",
        source="web",
    )
    assert final["status"] == "done"
    assert final["rewritten_script"] == "改写后的稿子"
    assert final["tts_audio_url"] == "https://oss.example.com/audio.mp3"


@pytest.mark.asyncio
async def test_run_distill_via_agent_failure_propagates(patch_agent_runner_deps, monkeypatch):
    """CP-AGENT-RUNNER-RUN-FAIL：agent 失败 → final 含 error_kind + error，_persist 也被调。"""
    from agent import runner as runner_module
    from tasks import distill_task

    final_state = {
        "article_id": "art_test",
        "user_id": 999,
        "url": "https://x",
        "source": "web",
        "status": "failed",
        "current_step": "failed",
        "error": "LLM 持续超时",
        "error_kind": "timeout",
        "error_step": "rewrite",
    }

    async def fake_ainvoke_failed(state):
        return final_state

    monkeypatch.setattr(runner_module.agent_app, "ainvoke", fake_ainvoke_failed)

    persist_called_with: list = []

    async def fake_persist(task_id, article_id, user_id, final):
        persist_called_with.append(final)

    monkeypatch.setattr(distill_task, "_persist_agent_final", fake_persist)

    final = await distill_task._run_distill_via_agent(
        task_id="dst_test_002",
        article_id="art_test_002",
        user_id=999,
        url="https://example.com",
        raw_content="原文",
    )
    assert final["status"] == "failed"
    assert final["error_kind"] == "timeout"
    assert final["error_step"] == "rewrite"
    # _persist_agent_final 仍然被调，让失败 → DB 写入完整
    assert len(persist_called_with) == 1
    assert persist_called_with[0]["error_kind"] == "timeout"


def test_distill_task_uses_agent_app():
    """CP-AGENT-RUNNER-INTEGRATION-CHECK：distill_task 引入 agent_app 而非 pipeline。"""
    from tasks import distill_task

    # 检查源码含 "_run_distill_via_agent" 调用而不是 pipeline.run
    import inspect

    src = inspect.getsource(distill_task.distill_task)
    assert (
        "_run_distill_via_agent" in src
    ), "distill_task 应该调用 _run_distill_via_agent（CP-AGENT-RUNNER-INTEGRATION）"
