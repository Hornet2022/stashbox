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


async def test_distill_task_uses_real_raw_content_from_db(
    article_with_raw_content, test_user, monkeypatch
):
    """DB 里有真 FetchResult → Step 1 收到的是 content_text，不是占位文本。"""
    recorder = RecordingLLM(dt_module.get_llm_client())
    monkeypatch.setattr(dt_module, "get_llm_client", lambda: recorder)

    result = await dt_module.distill_task(
        CTX,
        "dst_test0000000000000000001",
        article_with_raw_content.id,
        test_user,
        article_with_raw_content.url,
        title=article_with_raw_content.title,
    )

    assert result == {"task_id": "dst_test0000000000000000001", "status": "done"}

    # Step 1 的 user message 里必须出现真抓到的正文（占位文本已删）
    step1_prompt = recorder.requests[0].messages[-1].content
    assert "测试真内容 1+2=3" in step1_prompt
    assert "CP3.5 抓取器未接入" not in step1_prompt
