"""空 content 的根治：非流式空了就切流式。

## 事故与根因

2026-10-21 内测（uid=16）报蒸馏失败。根因是**上游 vLLM 的非流式路径**上
「增量解码 → 累积进 content」这步有缺陷：模型**正常结束**了
（`finish_reason="stop"`、`completion_tokens` 有值），但 content 一个字都没有。

实测对照（同一个 2925 字 prompt，tertu.cn 的 Qwen3.8-27B）：

    非流式 → 4/4 返回 content=""（空）
    流式   → 0/4 空，内容 756~1018 字

**流式把累积交给客户端**，正好绕开那个缺陷。

⚠️ 注意这**不是**上下文窗口的问题：2925 字 ≈ 2K token，32K 窗口远未超限；
`finish_reason` 也不是 "length"（那才是输出额度限制）。所以调大 timeout
没用（空 content 的响应 wall 只有 5~9 秒）、拆分 prompt 也没用
（退避 30s 0/8、改 temperature 5/5 全空）—— **只有换传输方式才有效**。
"""

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
def llm_mod():
    import importlib

    if "llm" not in sys.modules:
        importlib.import_module("llm")
    return importlib.import_module("llm.openai")


def _resp(content: str, completion_tokens: int = 0) -> dict:
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
    }


@pytest.mark.asyncio
async def test_empty_content_falls_back_to_stream(llm_mod, monkeypatch):
    """非流式返回空 content（有 token）→ 自动切流式拿到内容。"""
    client = llm_mod.OpenAIClient(api_key="k", base_url="http://x", model="m")

    calls: list[str] = []

    async def _post(body):
        calls.append("non_stream")
        # 上游的坏行为：模型生成了 token，但 content 是空的
        return _resp("", completion_tokens=426)

    async def _post_stream(body):
        calls.append("stream")
        return _resp('{"hook": "钩子", "sections": ["节拍"]}', completion_tokens=512)

    monkeypatch.setattr(client, "_post", _post)
    monkeypatch.setattr(client, "_post_stream", _post_stream)

    from llm.types import ChatMessage, ChatRequest

    resp = await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="原文" * 500)]))

    assert "钩子" in resp.content, resp.content
    assert calls[0] == "non_stream"
    assert calls[1] == "stream", "空 content 后必须切流式"
    assert resp.usage.completion_tokens == 512


@pytest.mark.asyncio
async def test_stream_is_tried_before_repeated_non_stream(llm_mod, monkeypatch):
    """流式要**先**于重复非流式重试 —— 上游空 content 概率约 40%，
    每次空都要等 4 次尝试 × ~8 秒；先试流式最坏只多一次请求。"""
    client = llm_mod.OpenAIClient(api_key="k", base_url="http://x", model="m")
    calls: list[str] = []

    async def _post(body):
        calls.append("non_stream")
        return _resp("", completion_tokens=100)

    async def _post_stream(body):
        calls.append("stream")
        return _resp("有内容", completion_tokens=100)

    monkeypatch.setattr(client, "_post", _post)
    monkeypatch.setattr(client, "_post_stream", _post_stream)

    from llm.types import ChatMessage, ChatRequest

    await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    assert calls == ["non_stream", "stream"], calls


@pytest.mark.asyncio
async def test_legit_empty_response_is_not_retried(llm_mod, monkeypatch):
    """真正的空回复（**没有**生成 token）→ 不浪费配额重试。"""
    client = llm_mod.OpenAIClient(api_key="k", base_url="http://x", model="m")
    calls: list[str] = []

    async def _post(body):
        calls.append("non_stream")
        return _resp("", completion_tokens=0)

    async def _post_stream(body):
        calls.append("stream")
        return _resp("", completion_tokens=0)

    monkeypatch.setattr(client, "_post", _post)
    monkeypatch.setattr(client, "_post_stream", _post_stream)

    from llm.types import ChatMessage, ChatRequest

    resp = await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    assert resp.content == ""
    assert calls == ["non_stream"], "没有 token 说明是真空，不该重试"


@pytest.mark.asyncio
async def test_stream_failure_falls_back_to_normal_retries(llm_mod, monkeypatch):
    """流式本身失败（网络/上游）→ 退回常规非流式重试，不能整体挂掉。"""
    client = llm_mod.OpenAIClient(api_key="k", base_url="http://x", model="m")
    calls: list[str] = []

    async def _post(body):
        calls.append("non_stream")
        return _resp("", completion_tokens=100)

    async def _post_stream(body):
        calls.append("stream")
        raise RuntimeError("流式连接断了")

    monkeypatch.setattr(client, "_post", _post)
    monkeypatch.setattr(client, "_post_stream", _post_stream)

    from llm.types import ChatMessage, ChatRequest

    resp = await client.chat(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    # 流式挂了 → 常规重试也空 → 最终返回空（上层会报错，不会把 repr 当内容）
    assert resp.content == ""
    assert "stream" in calls
    assert calls.count("non_stream") >= 2, "流式失败后应继续常规重试"
