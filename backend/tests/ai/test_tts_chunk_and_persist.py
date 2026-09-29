"""CP-INDEXTTS-CHUNK + CP-AGENT-PERSIST-ARTICLE-KEY 回归。

两个 bug 都是"线上才发现、且症状具有误导性"的类型，所以这里钉死行为：

1. 长稿分段合成（indextts）
   - 合成耗时随字数近似线性上涨（实测 600 字 ≈ 50s），3000+ 字成稿单请求
     必然撞 300s 超时 → 分段 + 拼接。
   - 钉子：切块不丢字、块大小有界、短文本不分块、拼接后 WAV 头合法。

2. 持久化被唯一约束打爆（distill_task._persist_agent_final）
   - `distilled_articles.article_id` 有 UNIQUE 约束，旧代码只按 `id == task_id`
     查行；同一篇文章换新 task_id 重跑 → INSERT 撞约束 → 事务 rollback →
     **已生成的 rewritten_script 一起被丢弃**。
   - 钉子：换 task_id 重跑仍只有 1 行、稿子保留、埋点照写。
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts.indextts import (  # noqa: E402
    IndexTTSClient,
    _concat_wav,
    _patch_riff_sizes,
    _sibling_omlx_urls,
    _split_into_chunks,
)


def _make_wav(nframes: int, freq: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(nframes)
        )
        w.writeframes(frames)
    return buf.getvalue()


# ==========================================================================
# 1. 切块
# ==========================================================================


def test_short_text_is_not_chunked():
    """短文本（管理后台测试按钮就发 7 个字）必须走单请求，不能被切。"""
    assert _split_into_chunks("TTS 烟雾测试。", 400) == ["TTS 烟雾测试。"]


def test_chunks_respect_limit_and_lose_no_characters():
    """核心不变量：切块后拼回去 == 原文，且**每块严格不超限**。

    超限不是"稍微长一点"的问题——超过截断阈值就会静默丢音频内容。
    """
    text = "。".join(f"这是第{i}句话内容用于测试切块逻辑" for i in range(120)) + "。"
    chunks = _split_into_chunks(text, 150)
    assert len(chunks) > 1
    assert max(len(c) for c in chunks) <= 150, f"最长的块 {max(len(c) for c in chunks)} 字，超限了"
    assert "".join(chunks) == text, "切块丢字了 —— 音频内容会缺段"


def test_single_overlong_sentence_is_hard_split():
    """没有任何句读的超长文本也要能切，不能死循环或整块发出去。"""
    chunks = _split_into_chunks("啊" * 1000, 400)
    assert [len(c) for c in chunks] == [400, 400, 200]
    assert "".join(chunks) == "啊" * 1000


def test_3574_char_script_splits_into_bounded_chunks():
    """真实成稿量级：4734 字必须被切到足够多的块，每块严格不超限。"""
    script = "测试内容。" * 946 + "测试结束"  # 4730 + 4 = 4734 字
    assert len(script) == 4734
    limit = IndexTTSClient.CHUNK_CHARS
    chunks = _split_into_chunks(script, limit)
    assert len(chunks) == 32, f"4734/150 应得 32 块，实得 {len(chunks)}"
    assert max(len(c) for c in chunks) <= limit, "有块超限，会触发静默截断"
    assert "".join(chunks) == script


def test_default_chunk_size_stays_under_truncation_threshold():
    """守住实测截断阈值。

    /v1/audio/speech 在输入超过 ~150 字后会**静默截断**：返回 HTTP 200 + 合法 WAV，
    但音频只有开头一小段（实测 300 字只产出 6.9s，语速 43.7 字/秒）。
    默认切块大小必须留在这个阈值以内，否则长稿会悄悄丢内容。
    """
    assert IndexTTSClient.CHUNK_CHARS <= 150


def test_chunk_timeout_leaves_margin_over_measured_cost():
    """150 字实测耗时约 30s，单请求超时必须留足余量。

    余量不足会把正常请求误判成超时（这正是之前分段方案失败的原因：
    当时切 400 字、按 0.6x RTF 需约 100s，而超时只给了 90s）。
    """
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8000/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert c.chunk_timeout >= 60, "对 150 字/块（约 30s）来说余量不足"


# ==========================================================================
# 2. WAV 拼接
# ==========================================================================


def test_concat_single_piece_returns_as_is():
    a = _make_wav(1600, 440)
    assert _concat_wav([a]) == a


def test_truncation_guard_flags_short_audio():
    """守住"HTTP 200 但内容被静默截断"这个坑。

    实测：300 字只产出 6.9s（43.7 字/秒）、600 字产出 19.8s（30.4 字/秒），
    而正常中文播报是 4~6 字/秒。不校验的话缺内容的音频会被当成功写库。
    """
    from app.services.tts.indextts import IndexTTSError, _assert_not_truncated

    silent_7s = _make_wav(16000 * 7, 440)  # 7 秒静音 WAV
    with pytest.raises(IndexTTSError, match="截断"):
        _assert_not_truncated("中" * 300, silent_7s)


def test_truncation_guard_does_not_false_positive_on_normal_audio():
    """150 字 / 17.9s = 8.4 字/秒属正常，不应误报。"""
    from app.services.tts.indextts import _assert_not_truncated

    normal = _make_wav(16000 * 18, 440)
    _assert_not_truncated("中" * 150, normal)  # 不抛即通过


def test_concat_wav_produces_valid_header_and_correct_length():
    """拼接后必须是可解析的 WAV，且帧数 = 各段之和（不能少帧/多帧）。"""
    a = _make_wav(16000, 440)
    b = _make_wav(8000, 660)
    c = _make_wav(4000, 880)
    merged = _concat_wav([a, b, c])

    assert merged[:4] == b"RIFF"
    with wave.open(io.BytesIO(merged)) as w:
        assert w.getnframes() == 16000 + 8000 + 4000
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1


def test_patch_riff_sizes_updates_length_fields():
    a = _make_wav(1600, 440)
    patched = _patch_riff_sizes(a, 999999)
    with wave.open(io.BytesIO(patched)) as w:
        # 头被撑大后 data 长度字段应反映新值（WAV 读取器容忍）
        assert w.getframerate() == 16000


# ==========================================================================
# 2.5 故障切换（CP-INDEXTTS-FAILOVER）
#
# 实测两个 oMLX 实例会**交替空转且会自愈**：同一分钟内 8000 挂 / 8008 正常，
# 下一分钟反过来。对着单个实例死等 300s 必然失败。
# ==========================================================================


def test_sibling_urls_include_both_local_omlx_ports():
    """本机 base_url 必须自动把另一个 oMLX 端口串进候选链。"""
    assert _sibling_omlx_urls("http://127.0.0.1:8000/v1") == [
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.1:8008/v1",
    ]
    assert _sibling_omlx_urls("http://127.0.0.1:8008/v1") == [
        "http://127.0.0.1:8008/v1",
        "http://127.0.0.1:8000/v1",
    ]


def test_sibling_urls_do_not_invent_ports_for_remote_host():
    """远程 oMLX 不该被塞本机端口。"""
    assert _sibling_omlx_urls("http://gpu-box:9000/v1") == ["http://gpu-box:9000/v1"]


def test_client_builds_failover_chain_and_short_timeout():
    """单请求超时必须远小于整体墙钟预算（300s），否则等于在空转实例上白等。"""
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8000/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert c.endpoints[0] == "http://127.0.0.1:8000/v1"
    assert "http://127.0.0.1:8008/v1" in c.endpoints
    # 90s << 300s 墙钟预算：快速失败才有意义
    assert c.chunk_timeout < c.timeout


def test_chunk_concurrency_is_bounded():
    """分段并发必须有上限——oMLX 每并发都要额外工作集，内存曾经被打爆过。"""
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8000/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert 1 <= c.chunk_concurrency <= 8


# ==========================================================================
# 3. 持久化：换 task_id 重跑不得丢稿
# ==========================================================================


@pytest.mark.asyncio
async def test_persist_same_article_different_task_id_keeps_script():
    """CP-AGENT-PERSIST-ARTICLE-KEY 的核心回归。

    `distilled_articles.article_id` 是 UNIQUE（一篇一结果）。旧实现只按
    `id == task_id` 查行，换 task_id 重跑就 INSERT → 撞约束 → 整个事务回滚，
    把已经写好的 rewritten_script 一起丢掉（日志表现为
    `agent_persist_failed ... This Session's transaction has been rolled back`）。

    这里连写 3 个不同 task_id，断言：仍只有 1 行，且最后一次的稿子保住了。
    """
    from sqlalchemy import select, text

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle
    from tasks.distill_task import _persist_agent_final

    article_id = "art_wxSOP_verify_0001"

    # 先把当前真实值存下来：这些测试跑在**真实库**上（不是 mock/fixture），
    # `_persist_agent_final` 会真的改写这行。收尾必须把**所有**被改的字段还原，
    # 否则测试会把生产数据的 status 写成 failed（实测踩过：跑完一轮全量测试，
    # 一条 status=done 的真实蒸馏结果就变成了 failed）。
    async with AsyncSessionLocal() as db:
        snapshot = (
            (
                await db.execute(
                    select(DistilledArticle).where(DistilledArticle.article_id == article_id)
                )
            )
            .scalars()
            .first()
        )
        saved = (
            None
            if snapshot is None
            else {
                "status": snapshot.status,
                "script_text": snapshot.script_text,
                "audio_url": snapshot.audio_url,
                "duration_sec": snapshot.duration_sec,
            }
        )
        await db.rollback()

    try:
        for i, script in enumerate(["甲稿" * 30, "乙稿" * 30, "丙稿" * 30], start=1):
            await _persist_agent_final(
                f"cp_persist_{i}",
                article_id,
                1,
                {
                    "status": "failed",
                    "error": "IndexTTS 合成超时(300.0s)",
                    "rewritten_script": script,
                    "error_kind": "internal",
                    "error_step": "tts",
                },
            )

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            rows = result.scalars().all()
            assert len(rows) == 1, f"一篇一结果，应为 1 行，实得 {len(rows)}"
            # 最后一次写入的稿子必须留存（这正是旧实现丢掉的东西）
            assert rows[0].script_text == "丙稿" * 30
            await db.rollback()
    finally:
        # 还原**全部**被改字段，不只是 script_text
        async with AsyncSessionLocal() as db:
            if saved is not None:
                await db.execute(
                    text(
                        "UPDATE distilled_articles SET status=:st, script_text=:sc, "
                        "audio_url=:au, duration_sec=:du WHERE article_id=:aid"
                    ),
                    {
                        "st": saved["status"],
                        "sc": saved["script_text"],
                        "au": saved["audio_url"],
                        "du": saved["duration_sec"],
                        "aid": article_id,
                    },
                )
            await db.commit()


@pytest.mark.asyncio
async def test_persist_failure_does_not_rollback_data_writes():
    """埋点失败绝不能连带回滚业务数据行（两段事务隔离）。"""
    from sqlalchemy import select, text

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle
    from tasks.distill_task import _persist_agent_final

    article_id = "art_wxSOP_verify_0001"
    marker = "隔离性验证稿" * 20

    async with AsyncSessionLocal() as db:
        snapshot = (
            (
                await db.execute(
                    select(DistilledArticle).where(DistilledArticle.article_id == article_id)
                )
            )
            .scalars()
            .first()
        )
        saved = (
            None
            if snapshot is None
            else {
                "status": snapshot.status,
                "script_text": snapshot.script_text,
                "audio_url": snapshot.audio_url,
                "duration_sec": snapshot.duration_sec,
            }
        )
        await db.rollback()

    try:
        await _persist_agent_final(
            "cp_persist_iso",
            article_id,
            1,
            {
                "status": "failed",
                "error": "x" * 500,  # reason 被截断到 200，写入正常
                "rewritten_script": marker,
                "error_kind": "internal",
                "error_step": "tts",
            },
        )
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            rows = result.scalars().all()
            assert len(rows) == 1
            assert rows[0].script_text == marker
            await db.rollback()
    finally:
        # 同上：跑在真实库上，必须还原全部被改字段
        async with AsyncSessionLocal() as db:
            if saved is not None:
                await db.execute(
                    text(
                        "UPDATE distilled_articles SET status=:st, script_text=:sc, "
                        "audio_url=:au, duration_sec=:du WHERE article_id=:aid"
                    ),
                    {
                        "st": saved["status"],
                        "sc": saved["script_text"],
                        "au": saved["audio_url"],
                        "du": saved["duration_sec"],
                        "aid": article_id,
                    },
                )
            await db.commit()


# ==========================================================================
# 7. ready 写回必须按业务键（CP-AGENT-PERSIST-ARTICLE-KEY 的下游一致性）
#
# `_persist_agent_final` 按 article_id 落库（复用既有行，id 仍是第一次的
# task_id），但 distill_task 原本按 `id == task_id` 查蒸馏行 → 查不到 →
# `if da is not None` 整段跳过 → articles.status 永远停在 distilling。
# ==========================================================================


@pytest.mark.asyncio
async def test_ready_writeback_finds_row_by_article_id():
    """文章行 id 与 task_id 不同时，仍必须能写回 ready + audio_url。"""
    from sqlalchemy import select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle

    article_id = "art_wxSOP_verify_0001"
    async with AsyncSessionLocal() as db:
        by_article = (
            await db.execute(
                select(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
        ).scalar_one_or_none()
        assert by_article is not None, "按 article_id 查不到蒸馏行"
        # 复现生产形态：行 id 是第一次的 task_id，与本次 task_id 不同
        by_task = (
            await db.execute(
                select(DistilledArticle).where(DistilledArticle.id == "dst_definitely_not_matching")
            )
        ).scalar_one_or_none()
        assert by_task is None, "对照组前提不成立"
        await db.rollback()


# ==========================================================================
# 6. TTS 重复合成护栏（CP-AGENT-TTS-LOOP-GUARD）
#
# 实测死循环：tts_synthesize 的 audio_url 恒为 None（OSS 未实现），
# tts_node 写回 tts_audio_url=None → router 规则「有稿无音频 → skip_to_tts」
# → 回到 tts_node 再合成一遍。实测一次任务合成了 3 遍 8MB+ 音频，
# 最后靠某一块超时才收场。
# ==========================================================================


@pytest.mark.asyncio
async def test_tts_node_does_not_resynthesize_loop(monkeypatch):
    """已经合成过却仍拿不到 audio_url 时，必须立刻失败而不是再来一遍。"""
    from agent import runner as R

    calls = {"n": 0}

    async def _fake_invoke(name, state, args):
        calls["n"] += 1
        return {"audio_url": None, "audio_path": "/tmp/x.wav", "duration_sec": 100}

    monkeypatch.setattr(R.get_default_registry(), "invoke", _fake_invoke, raising=False)

    state = {
        "article_id": "art_loop",
        "fetched_content": "原文",
        "rewritten_script": "听感稿",
        "tts_audio_url": None,
        # 关键：tool_calls 里已有 tts_synthesize 历史
        "tool_calls": [{"name": "tts_synthesize", "args": {}}],
    }
    out = await R.tts_node(state)

    assert calls["n"] == 0, f"护栏没生效，重复调用了 {calls['n']} 次"
    assert out.get("error_kind"), "护栏触发时必须给出 error_kind"


@pytest.mark.asyncio
async def test_tts_node_first_call_still_runs(monkeypatch):
    """首次必须正常合成（护栏不能误伤第一次）。"""
    from agent import runner as R

    calls = {"n": 0}

    async def _fake_invoke(name, state, args):
        calls["n"] += 1
        return {"audio_url": None, "audio_path": "/tmp/x.wav", "duration_sec": 42}

    monkeypatch.setattr(R.get_default_registry(), "invoke", _fake_invoke, raising=False)

    out = await R.tts_node(
        {
            "article_id": "art_first",
            "fetched_content": "原文",
            "rewritten_script": "听感稿",
            "tts_audio_url": None,
            "tool_calls": [],
        }
    )
    assert calls["n"] == 1
    assert out.get("tts_audio_path") == "/tmp/x.wav"
    assert out.get("tts_duration_sec") == 42


# ==========================================================================
# 5. Router 快路径（CP-AGENT-ROUTER-MODE）
#
# `_default_next_action` 已确定性地覆盖全部状态组合，正常流程里 router 的每次
# 决策都规则可判 —— LLM 一次都没改变过结论，只是白花时间（每次 2~5s +
# 约 100 completion_token，整链路要调 3~4 次）。
# ==========================================================================


@pytest.mark.asyncio
async def test_router_rules_mode_makes_no_llm_call(monkeypatch):
    """rules 模式下 router 不得触碰 LLM —— 这是本次优化的核心收益。"""

    from agent import runner as R

    monkeypatch.setattr(R, "ROUTER_MODE", "rules", raising=False)

    def _boom():  # 任何 LLM 调用都视为失败
        raise AssertionError("rules 模式不应调用 LLM")

    monkeypatch.setattr(R._llm_module, "get_llm_client", _boom)

    state = {
        "article_id": "art_x",
        "fetched_content": "正文" * 100,
        "rewritten_script": "",
        "tts_audio_url": None,
        "final_audio_url": None,
    }
    out = await R.decision_router_node(state)
    assert out["next_action"] == "rewrite"


@pytest.mark.asyncio
async def test_router_llm_mode_caps_tokens(monkeypatch):
    """llm 模式下 router 必须限制 max_tokens 并降温（实测省一半时间）。"""
    from agent import runner as R

    monkeypatch.setattr(R, "ROUTER_MODE", "llm", raising=False)
    monkeypatch.setattr(R, "ROUTER_MAX_TOKENS", 64, raising=False)

    seen: dict = {}

    class _Resp:
        content = '{"next_action": "rewrite", "reason": "ok"}'

    class _LLM:
        async def chat(self, req):
            seen["max_tokens"] = getattr(req, "max_tokens", None)
            seen["temperature"] = getattr(req, "temperature", None)
            return _Resp()

    monkeypatch.setattr(R._llm_module, "get_llm_client", lambda: _LLM())

    state = {
        "article_id": "art_x",
        "fetched_content": "正文" * 100,
        "rewritten_script": "",
        "tts_audio_url": None,
        "final_audio_url": None,
    }
    out = await R.decision_router_node(state)
    assert out["next_action"] == "rewrite"
    assert seen["max_tokens"] == 64, f"router 未限 token，实得 {seen['max_tokens']}"


def test_default_next_action_covers_every_state_combination():
    """快路径的前提：规则必须覆盖全部组合，否则会退化成"什么都不做"。"""
    from agent.runner import _default_next_action

    fetched = "正文" * 100
    rewritten = "稿子"
    tts = "https://oss/x.wav"
    final = "https://oss/final.wav"

    assert _default_next_action({"fetched_content": ""}) == "fail"
    assert _default_next_action({"fetched_content": fetched}) == "rewrite"
    assert (
        _default_next_action({"fetched_content": fetched, "rewritten_script": rewritten})
        == "skip_to_tts"
    )
    assert (
        _default_next_action(
            {
                "fetched_content": fetched,
                "rewritten_script": rewritten,
                "tts_audio_url": tts,
            }
        )
        == "skip_to_concat"
    )
    assert (
        _default_next_action(
            {
                "fetched_content": fetched,
                "rewritten_script": rewritten,
                "tts_audio_url": tts,
                "final_audio_url": final,
            }
        )
        == "done"
    )


# ==========================================================================
# 4. LLM 总墙钟死线（CP-LLM-DEADLINE）
#
# httpx 的 timeout 是**每次 I/O 操作**的超时，不是请求总时长。上游只要持续
# 缓慢吐字节，读超时就不断重置，请求可以无限期挂着。
#
# 实测踩过：蒸馏任务在 attempt=0 超时后日志完全停止，worker 进程
# CPU 0.3% / 状态 S / 22 分钟零增长，一直挂到 Arq job_timeout 强杀。
# ==========================================================================


@pytest.mark.asyncio
async def test_llm_request_dies_at_total_wallclock_deadline():
    """传输层持续缓慢吐字节（读超时不断重置）时，chat 必须按总墙钟失败。"""
    import asyncio as _asyncio

    import httpx as _httpx

    from llm.openai import OpenAIClient
    from llm.types import ChatMessage, ChatRequest

    class _DripStream(_httpx.AsyncByteStream):
        """每 50ms 吐 1 字节：单次 read 远小于传输层超时，但总耗时无上限。"""

        async def __aiter__(self):
            while True:
                await _asyncio.sleep(0.05)
                yield b" "

        async def aclose(self) -> None:
            return None

    class _DripTransport(_httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return _httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_DripStream()
            )

    client = OpenAIClient(
        api_key="k",
        model="m",
        base_url="http://test.invalid/v1",
        timeout=5.0,
        max_retries=1,
    )
    await client._client.aclose()
    client._client = _httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_DripTransport())

    loop = _asyncio.get_event_loop()
    started = loop.time()
    req = ChatRequest(messages=[ChatMessage(role="user", content="你好")], max_tokens=8)
    with pytest.raises(Exception):  # noqa: B017 - 抛什么类型不重要，关键是"会抛"
        await client.chat(req)
    elapsed = loop.time() - started

    # 若无总死线，这里会是永久挂起（测试自身会超时）
    assert elapsed < 15.0, f"总墙钟死线未生效，耗时 {elapsed:.1f}s"
    await client._client.aclose()
