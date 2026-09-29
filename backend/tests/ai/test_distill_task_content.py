"""CP3-CONTENT：distill_task 从 articles.raw_content 读真抓取内容。

覆盖 `_load_raw_content` 的 3 种返回 + distill_task 端到端（真 PG + 真 DistillPipeline）。
"""

import pytest

from tasks import distill_task as dt_module

CTX = {"job_id": "job-1", "redis": None}


# ---------------------------------------------------------------------------
# _load_raw_content 单元
# ---------------------------------------------------------------------------
async def test_load_raw_content_with_content_text(article_with_raw_content, db_session):
    """raw_content["content_text"] 非空 → 直接返回 content_text。"""
    result = await dt_module._load_raw_content(db_session, article_with_raw_content.id)

    assert result == "测试真内容 1+2=3"


async def test_load_raw_content_fallback_to_title_and_url(article_factory, db_session):
    """raw_content["content_text"] 空 → fallback 到 "[无正文] title=X url=Y"。"""
    art = await article_factory(
        {"content_text": "   ", "title": "空正文标题", "url": "https://example.com/empty"}
    )

    result = await dt_module._load_raw_content(db_session, art.id)

    assert result == "[无正文] title=空正文标题 url=https://example.com/empty"


async def test_load_raw_content_empty_raw_content(article_factory, db_session):
    """raw_content 为 NULL → "[empty article]"。"""
    art = await article_factory(None, title="无抓取结果")

    result = await dt_module._load_raw_content(db_session, art.id)

    assert result == f"[empty article] title=无抓取结果 url={art.url}"


async def test_load_raw_content_article_not_found_raises(db_session):
    """article_id 不存在 → 抛 ArticleNotFoundError（让 Arq 走 retry）。"""
    with pytest.raises(dt_module.ArticleNotFoundError):
        await dt_module._load_raw_content(db_session, "art_not_exists_000000000000")


# ---------------------------------------------------------------------------
# distill_task 端到端
# ---------------------------------------------------------------------------
class RecordingLLM:
    """包一层 get_llm_client() 的返回值：记录每次 chat 请求，再转发给真 mock LLM。"""

    def __init__(self, inner):
        self.inner = inner
        self.requests: list = []

    async def chat(self, req):
        self.requests.append(req)
        return await self.inner.chat(req)

    async def close(self) -> None:
        return await self.inner.close()


class _AgentFakeLLM:
    """CP-AGENT 同步：state-aware 替身。

    决策路由必须按当前 state 推进（否则图会 rewrite↔router 死循环），
    所以不能像旧 pipeline 那样「按 step 返回固定内容」。
    """

    def __init__(self) -> None:
        self.requests: list = []

    async def chat(self, req):
        self.requests.append(req)
        from llm.types import ChatResponse, Usage

        step = (req.metadata or {}).get("step")
        user_text = req.messages[-1].content if req.messages else ""

        if step == "agent_decision_router":
            if "已拼接" in user_text:
                content = '{"next_action": "done"}'
            elif "已合成" in user_text:
                content = '{"next_action": "skip_to_concat"}'
            elif "已改写" in user_text:
                content = '{"next_action": "skip_to_tts"}'
            else:
                content = '{"next_action": "rewrite"}'
        else:
            content = '{"hook":"开场","sections":["主体第一段"],"outro":"结尾","word_count":10}'

        return ChatResponse(content=content, model="fake-agent", usage=Usage())

    async def close(self) -> None:
        return None


async def test_distill_task_uses_real_raw_content_from_db(
    article_with_raw_content, test_user, monkeypatch
):
    """DB 里有真 FetchResult → agent rewrite 收到的是 content_text，不是占位文本。

    CP-AGENT-RUNNER-INTEGRATION 同步：distill_task 现在走 LangGraph agent
    （fetch → decision_router → rewrite → ...），rewrite_node 调
    `llm.chat(ChatRequest)`。这里用 state-aware 替身记录 ChatRequest，
    断言 user message 里出现真抓到的正文。
    """
    recorder = _AgentFakeLLM()

    # agent runner 通过 llm.get_llm_client()（动态模块引用）拿 client，
    # 所以要 patch ai-service/llm 模块的属性，而不是 dt_module.get_llm_client
    import llm as llm_module

    # agent 里是 `await get_llm_client()`，所以替身必须是 async 函数
    def _fake_get_llm_client():
        return recorder

    monkeypatch.setattr(llm_module, "get_llm_client", _fake_get_llm_client)

    # CP-AGENT 同步：agent 的 tts_node 会调 tts_synthesize tool（真实 TTS）。
    # 本用例只关心 rewrite prompt 内容，这里把 TTS tool 打成替身避免打真 TTS 网络。
    from agent import tools as agent_tools

    agent_tools._default_registry_singleton = None

    async def _fake_tts(state, args):
        return {"audio_url": "https://oss.example.com/a.mp3", "voice": "v", "duration_sec": 10}

    monkeypatch.setattr(agent_tools, "tts_synthesize_tool", _fake_tts)

    result = await dt_module.distill_task(
        CTX,
        "dst_test0000000000000000001",
        article_with_raw_content.id,
        test_user,
        article_with_raw_content.url,
        title=article_with_raw_content.title,
    )

    assert result == {"task_id": "dst_test0000000000000000001", "status": "done"}

    # agent rewrite 的 user message 必须出现真抓到的正文（占位文本已删）
    prompts = [
        req.messages[-1].content for req in recorder.requests if getattr(req, "messages", None)
    ]
    assert any("测试真内容 1+2=3" in p for p in prompts), prompts
    assert not any("CP3.5 抓取器未接入" in p for p in prompts), prompts
