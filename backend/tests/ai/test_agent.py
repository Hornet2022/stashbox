"""CP-AGENT-* 听匣 agent 模块单测（Phase 1 + 2 骨架）。

覆盖范围：
  - AgentState TypedDict 字段定义正确
  - ToolRegistry：register / invoke / list_specs
  - ToolError：kind 字段保留 + 节点 catch 时写入 state.error_kind
  - 4 个内置 tool：fetch_url / tts_synthesize / stage_cache_lookup / save_memory（mock LLM/TTS）
  - MemoryStore：inject_into_prompt 把 profile + few-shot 注入到 base_prompt
  - LangGraph runner：build_agent_graph 返回 5 节点 + 边；mock tool 后 invoke 能跑通 4 步
  - 失败路径：fetch_url 抛 ToolError → 路由到 failed_node，state.error_kind 有值

注意：tool 实现里调真实 DB / LLM / TTS 的部分用 monkeypatch 隔离；
rewrite_node 调 llm.chat() 时也 monkeypatch 掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 把 ai-service 加到 path 让 agent 模块能找到 stage_cache 等兄弟模块
_AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_AI_SERVICE))


# ---------------------------------------------------------------------------
# AgentState / TypedDict 字段测试
# ---------------------------------------------------------------------------


def test_agent_state_importable():
    from agent.state import AgentState

    # AgentState 是 TypedDict，annotated 字段都能拿到
    annotations = AgentState.__annotations__
    # 必填字段
    for k in ("article_id", "user_id", "url", "source"):
        assert k in annotations, f"AgentState 缺字段 {k}"
    # 流程控制
    for k in ("current_step", "status", "next_action"):
        assert k in annotations
    # 上下文
    for k in ("fetched_content", "rewritten_script", "tts_audio_url", "final_audio_url"):
        assert k in annotations
    # 工具结果
    for k in ("tool_calls", "tool_results"):
        assert k in annotations
    # 错误（与 _classify_*_error 兼容）
    for k in ("error", "error_step", "error_kind", "retry_after"):
        assert k in annotations
    # 记忆
    for k in ("user_profile", "few_shot_examples"):
        assert k in annotations


# ---------------------------------------------------------------------------
# ToolRegistry 测试
# ---------------------------------------------------------------------------


def test_tool_registry_register_and_invoke():
    from agent.tools import ToolRegistry, ToolSpec

    reg = ToolRegistry()

    async def my_tool(state, args):
        return {"ok": True, "echo": args.get("msg", "")}

    reg.register(ToolSpec(name="echo", description="回显测试", func=my_tool))
    assert len(reg.list_specs()) == 1

    # invoke
    import asyncio

    result = asyncio.run(reg.invoke("echo", {}, {"msg": "hi"}))
    assert result == {"ok": True, "echo": "hi"}


def test_tool_registry_invoke_unknown_raises():
    from agent.tools import ToolRegistry

    reg = ToolRegistry()
    with pytest.raises(KeyError, match="not registered"):
        reg.get("nope")


def test_tool_registry_invoke_wraps_internal_exception():
    """CP-AGENT-TOOLS-INVOKE-ERR：tool 抛非 ToolError 时，registry 包成 ToolError('internal')。"""
    from agent.tools import ToolRegistry, ToolSpec, ToolError

    reg = ToolRegistry()

    async def boom(state, args):
        raise RuntimeError("boom")

    reg.register(ToolSpec(name="boom", description="x", func=boom))

    import asyncio

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(reg.invoke("boom", {}, {}))
    assert exc_info.value.kind == "internal"


def test_default_registry_has_4_tools():
    from agent.tools import get_default_registry

    names = {s.name for s in get_default_registry().list_specs()}
    assert names == {"fetch_url", "tts_synthesize", "stage_cache_lookup", "save_memory"}


@pytest.fixture(autouse=True)
def reset_default_registry_singleton():
    """每次测试前清空 lazy singleton，让 monkeypatch 的 func 在下次 register() 时生效。"""
    from agent import tools as tools_module

    tools_module._default_registry_singleton = None
    yield
    tools_module._default_registry_singleton = None


def test_tool_spec_to_openai_tool_schema():
    """Phase 3 router 需要这个 schema 暴露给 LLM。"""
    from agent.tools import ToolSpec

    async def f(state, args):
        return {}

    spec = ToolSpec(name="x", description="y", func=f)
    schema = spec.to_openai_tool()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "x"
    assert schema["function"]["description"] == "y"
    assert "parameters" in schema["function"]


# ---------------------------------------------------------------------------
# Memory 测试
# ---------------------------------------------------------------------------


def test_user_profile_to_prompt_fragment_basic():
    from agent.memory import UserProfile

    p = UserProfile(user_id=1, tier="pro")
    fragment = p.to_prompt_fragment()
    assert "用户等级：pro" in fragment


def test_user_profile_to_prompt_fragment_with_ab_and_prefs():
    from agent.memory import UserProfile

    p = UserProfile(
        user_id=1,
        tier="member",
        ab_group="personalized",
        preferences={"length_pref": "短", "style": "口语"},
    )
    fragment = p.to_prompt_fragment()
    assert "用户等级：member" in fragment
    assert "A/B 分组：personalized" in fragment
    assert "偏好·length_pref：短" in fragment


def test_user_profile_to_prompt_fragment_free_no_prefs_emits_nothing_extra():
    """free 用户且无 preferences 时不应注入额外 prompt 噪声。"""
    from agent.memory import UserProfile

    p = UserProfile(user_id=1, tier="free")
    fragment = p.to_prompt_fragment()
    assert fragment.strip() == "用户等级：free"


def test_few_shot_to_prompt_pair():
    from agent.memory import FewShotExample

    ex = FewShotExample(
        input_excerpt="今天天气...",
        output_excerpt="各位听众，今天...",
        score=8.5,
    )
    pair = ex.to_prompt_pair()
    assert "[输入]" in pair and "[输出]" in pair
    assert "今天天气" in pair


def test_memory_store_inject_merges_base_prompt():
    from agent.memory import MemoryStore, UserProfile, FewShotExample

    store = MemoryStore()
    p = UserProfile(user_id=1, tier="pro")
    e = [
        FewShotExample(input_excerpt="i", output_excerpt="o", score=8.0),
    ]
    base = "请改写。"
    merged = store.inject_into_prompt(p, e, base)
    # 有 profile + 有 examples → 应该有"# 用户偏好" + "# Few-shot 样本" + "# 当前任务"
    assert "# 用户偏好" in merged
    assert "# Few-shot 样本" in merged
    assert "# 当前任务" in merged
    assert base in merged


def test_memory_store_inject_no_memory_returns_base():
    from agent.memory import MemoryStore

    store = MemoryStore()
    base = "no injection"
    merged = store.inject_into_prompt(None, [], base)
    assert merged == base


# ---------------------------------------------------------------------------
# LangGraph runner 测试（mock 掉 LLM + tool 内部依赖）
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_agent_deps(monkeypatch):
    """monkeypatch agent 内部用到的：fetch_url_tool、tts_synthesize_tool、get_llm_client。

    让测试可控：
      - fetch_url_tool 返回固定 content
      - tts_synthesize_tool 返回固定 audio_url
      - llm.chat() 根据 prompt 区分：decision_router → JSON；rewrite_node → 文本
    """
    # import 在 fixture 里做（pytest collection 顺序避免循环 import）
    from agent import runner as runner_module
    from agent import tools as tools_module

    # 先清空 lazy singleton：下次 get_default_registry() 会用 monkeypatched func 重新注册
    tools_module._default_registry_singleton = None

    async def fake_fetch_url(state, args):
        return {
            "content": "今天苹果发布了新款 iPhone。",
            "meta": {"title": "新闻", "author": "Alice", "word_count": 100},
            "source_type": state.get("source", "generic_url"),
        }

    async def fake_tts(state, args):
        return {
            "audio_url": "https://oss.example.com/audio.mp3",
            "voice": "default",
            "duration_sec": 60,
        }

    def _prompt_text(req) -> str:
        """从 ChatRequest 里取最后一条 user 消息文本（兼容 str 传参）。"""
        if isinstance(req, str):
            return req
        msgs = getattr(req, "messages", None) or []
        for m in reversed(msgs):
            if getattr(m, "role", None) == "user":
                return getattr(m, "content", "") or ""
        return ""

    async def fake_llm_chat(prompt: str) -> str:
        # CP-AGENT-ROUTER-DISPATCH：Phase 3 决策路由器 + rewrite_node 都用 chat()；
        # 测试时根据 prompt 区分：含 "orchestrator" / "next_action" 是 router。
        if "orchestrator" in prompt or "next_action" in prompt:
            # 决策路由器：根据 state 内容回 next_action
            if "已拼接" in prompt:
                return '{"next_action": "done", "reason": "全完成"}'
            if "已合成" in prompt:
                return '{"next_action": "skip_to_concat", "reason": "TTS 已生成"}'
            if "已改写" in prompt:
                return '{"next_action": "skip_to_tts", "reason": "rewritten_script 已生成"}'
            return '{"next_action": "rewrite", "reason": "fetched_content 已抓取"}'
        # rewrite_node：返回改写后的稿子
        return "各位听众，今天苹果发布了新款 iPhone，下面我们一起看看..."

    monkeypatch.setattr(tools_module, "fetch_url_tool", fake_fetch_url)
    monkeypatch.setattr(tools_module, "tts_synthesize_tool", fake_tts)

    # rewrite_node / decision_router_node 用 llm.get_llm_client() → llm.chat(ChatRequest)
    class _FakeLLM:
        async def chat(self, req):
            text = await fake_llm_chat(_prompt_text(req))
            # 返回 ChatResponse 兼容对象（agent 用 getattr(resp, "content", None)）
            return type("FakeChatResp", (), {"content": text})()

    def fake_get_llm_client():
        return _FakeLLM()

    # 在 ai-service/llm/__init__.py 里
    import llm as llm_module

    monkeypatch.setattr(llm_module, "get_llm_client", fake_get_llm_client)

    return runner_module, tools_module


def test_build_agent_graph_has_5_user_nodes():
    """CP-AGENT-RUNNER-BUILD：5 个用户节点 + 1 个 __start__ + 1 个 __end__。"""
    from agent.runner import build_agent_graph

    app = build_agent_graph()
    nodes = list(app.nodes.keys())
    assert "__start__" in nodes
    assert "fetch_node" in nodes
    assert "rewrite_node" in nodes
    assert "tts_node" in nodes
    assert "concat_node" in nodes
    assert "failed_node" in nodes


@pytest.mark.asyncio
async def test_agent_runs_full_pipeline_success(patch_agent_deps):
    """CP-AGENT-RUN-SUCCESS：4 步全跑通，state 有最终 audio_url。"""
    from agent.runner import agent_app

    initial = {
        "article_id": "art_test_001",
        "user_id": 999,
        "url": "https://example.com/test",
        "source": "web",
    }
    final = await agent_app.ainvoke(initial)
    assert final["status"] == "done"
    assert final["current_step"] == "done"
    assert final["fetched_content"] == "今天苹果发布了新款 iPhone。"
    assert "各位听众" in final["rewritten_script"]
    assert final["tts_audio_url"] == "https://oss.example.com/audio.mp3"
    assert final["final_audio_url"] == "https://oss.example.com/audio.mp3"
    assert final["final_duration_sec"] == 60
    # 工具调用历史至少 2 条（fetch + tts）
    assert len(final["tool_calls"]) == 2
    assert final["tool_calls"][0]["name"] == "fetch_url"
    assert final["tool_calls"][1]["name"] == "tts_synthesize"
    assert "finished_at" in final


@pytest.mark.asyncio
async def test_agent_fetch_failure_routes_to_failed_node(monkeypatch):
    """CP-AGENT-RUN-FAIL-FETCH：fetch 失败 → 路由到 failed_node，error_kind 保留。"""
    from agent import tools as tools_module

    async def fake_fetch_url_boom(state, args):
        raise tools_module.ToolError("network", "DNS 解析失败")

    monkeypatch.setattr(tools_module, "fetch_url_tool", fake_fetch_url_boom)

    from agent.runner import agent_app

    initial = {
        "article_id": "art_test_002",
        "user_id": 999,
        "url": "https://nonexistent.example",
        "source": "web",
    }
    final = await agent_app.ainvoke(initial)
    assert final["status"] == "failed"
    assert final["current_step"] == "failed"
    assert final["error_step"] == "fetch"
    assert final["error_kind"] == "network"
    assert "DNS" in final["error"]


@pytest.mark.asyncio
async def test_agent_rewrite_failure_routes_to_failed_node(patch_agent_deps, monkeypatch):
    """CP-AGENT-RUN-FAIL-REWRITE：rewrite 失败 → failed_node，error_kind='timeout'。"""
    from agent import runner as runner_module

    # patch runner_module 里的 _maybe_inject_memory（避免调真 MemoryStore）
    monkeypatch.setattr(
        runner_module,
        "_maybe_inject_memory",
        lambda state, base: base,
    )

    # patch LLM 抛 timeout
    from agent import tools as tools_module
    import llm as llm_module

    class _FakeLLM:
        async def chat(self, req):
            raise tools_module.ToolError("timeout", "LLM 持续超时")

    def fake_get_llm_client():
        return _FakeLLM()

    # patch_agent_deps 已经 monkeypatch 过 llm.get_llm_client，这里用 pytest fixture
    # monkeypatch 覆盖它
    monkeypatch.setattr(llm_module, "get_llm_client", fake_get_llm_client)

    from agent.runner import agent_app

    final = await agent_app.ainvoke(
        {
            "article_id": "art_test_003",
            "user_id": 999,
            "url": "https://example.com",
            "source": "web",
        }
    )
    assert final["status"] == "failed"
    assert final["error_step"] == "rewrite"
    assert final["error_kind"] == "timeout"


@pytest.mark.asyncio
async def test_agent_runs_with_user_profile_memory_injected(patch_agent_deps, monkeypatch):
    """CP-AGENT-MEMORY-INJECT-RUN：state 里带 user_profile + few_shot_examples → rewrite prompt 注入。"""
    import llm as llm_module

    captured: dict[str, str] = {}

    def _ptext(req):
        if isinstance(req, str):
            return req
        msgs = getattr(req, "messages", None) or []
        for m in reversed(msgs):
            if getattr(m, "role", None) == "user":
                return getattr(m, "content", "") or ""
        return ""

    class _FakeLLM:
        async def chat(self, req):
            # CP-AGENT-ROUTER-DISPATCH：decision_router_node 也用 chat()，
            # 测试要区分 router 提示词 vs rewrite_node 提示词。
            prompt = _ptext(req)
            if "orchestrator" in prompt or "next_action" in prompt:
                # router：根据 prompt 内容选 next_action
                if "已拼接" in prompt:
                    return type("R", (), {"content": '{"next_action": "done"}'})()
                if "已合成" in prompt:
                    return type("R", (), {"content": '{"next_action": "skip_to_concat"}'})()
                if "已改写" in prompt:
                    return type("R", (), {"content": '{"next_action": "skip_to_tts"}'})()
                return type("R", (), {"content": '{"next_action": "rewrite"}'})()
            # rewrite_node：抓 prompt 到 captured
            captured["prompt"] = prompt
            return type("R", (), {"content": "改写后的稿子"})()

    def fake_get_llm_client():
        return _FakeLLM()

    # patch_agent_deps 已经 monkeypatch 过 llm.get_llm_client，这里用 pytest fixture
    # monkeypatch 覆盖它，确保 pytest 自动还原
    monkeypatch.setattr(llm_module, "get_llm_client", fake_get_llm_client)

    from agent.runner import agent_app

    final = await agent_app.ainvoke(
        {
            "article_id": "art_test_004",
            "user_id": 999,
            "url": "https://example.com",
            "source": "web",
            "user_profile": {
                "user_id": 999,
                "tier": "pro",
                "ab_group": "personalized",
                "preferences": {"length_pref": "短"},
            },
            "few_shot_examples": [
                {
                    "input_excerpt": "原文片段",
                    "output_excerpt": "改写片段",
                    "score": 9.0,
                    "tags": ("科技",),
                }
            ],
        }
    )

    # rewrite prompt 应该包含 user_profile + few_shot
    assert "# 用户偏好" in captured["prompt"]
    assert "用户等级：pro" in captured["prompt"]
    assert "偏好·length_pref：短" in captured["prompt"]
    assert "# Few-shot 样本" in captured["prompt"]
    assert "评分 9.0" in captured["prompt"]
    # 同时跑完
    assert final["status"] == "done"


# ---------------------------------------------------------------------------
# CP-AGENT-FAILURE-PROPAGATION：agent 失败必须让 distill_task 走失败路径
# ---------------------------------------------------------------------------
def test_agent_failure_status_not_done_raises():
    """CP-AGENT-FAILURE-PROPAGATION：final.status != done → distill_task 抛异常。

    否则任务被误记为成功（不退还配额、articles 停在不一致状态）。
    """
    import inspect

    from tasks import distill_task as dt_module

    src = inspect.getsource(dt_module.distill_task)
    assert (
        'final or {}).get("status") != "done"' in src or 'status\') != "done"' in src
    ), "distill_task 必须显式检查 agent final.status，非 done 时抛异常"


@pytest.mark.asyncio
async def test_rewrite_llm_error_wrapped_as_timeout_kind(monkeypatch):
    """CP-AGENT-LLM-ERR-CLASSIFY：llm.exceptions.LLMError('... ReadTimeout') → kind=timeout。"""
    from agent import tools as tools_module
    import llm as llm_module

    tools_module._default_registry_singleton = None

    async def fake_fetch(state, args):
        return {"content": "内容", "meta": {}, "source_type": "web"}

    monkeypatch.setattr(tools_module, "fetch_url_tool", fake_fetch)

    class _FakeLLM:
        async def chat(self, req):
            # LLMClient 的真实包装形态
            raise RuntimeError("openai chat failed after 3 attempts: ReadTimeout('')")

    def _fake_get():
        return _FakeLLM()

    monkeypatch.setattr(llm_module, "get_llm_client", _fake_get)

    from agent.runner import agent_app

    final = await agent_app.ainvoke(
        {
            "article_id": "art_t",
            "user_id": 1,
            "url": "https://x.com",
            "source": "web",
            "fetched_content": "内容",
        }
    )
    assert final["status"] == "failed"
    assert final["error_kind"] == "timeout", final
    assert final["error_step"] == "rewrite"
