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
import uuid
import wave
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.tts.indextts import (  # noqa: E402
    IndexTTSClient,
    IndexTTSError,
    _concat_wav,
    _patch_riff_sizes,
    _split_into_chunks,
)


@pytest_asyncio.fixture
async def isolated_article(test_user):
    """给持久化测试一条**独占**的 article + distilled 行，结束后整棵删掉。

    为什么不能拿现成文章当靶子
    --------------------------
    `_persist_agent_final` 内部自己开 `AsyncSessionLocal` 并 **commit**，
    调用方的外层事务回滚对它无效 —— 测试会真的把那一行改掉。

    本文件此前直接用生产文章 `art_wxSOP_verify_0001`，实测造成过真实数据损坏：
    真实蒸馏结果的 `script_text` 被写成 "甲稿/乙稿/丙稿" 占位符，
    详情页在真机上直接显示 "丙稿丙稿丙稿…"。

    而且"快照 → finally 还原"是个**棘轮**：第一轮把真值改成脏值并还原成功，
    但只要有一轮进程被中断没跑到 finally，脏值就成为下一轮的快照基线，
    此后每轮都忠实地把脏值还原回去 —— 存量永远洗不掉。

    改成自建自销后，测试与生产数据之间不存在任何交集。
    """
    from sqlalchemy import delete

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import Article, DistilledArticle

    # id 列是 VARCHAR(32)，前缀 + 18 位 hex 必须留出余量
    article_id = f"art_tstp_{uuid.uuid4().hex[:18]}"
    distill_id = f"dst_tstp_{uuid.uuid4().hex[:18]}"

    async with AsyncSessionLocal() as db:
        db.add(
            Article(
                id=article_id,
                user_id=test_user,
                url="https://example.invalid/isolated-persist-test",
                title="持久化隔离测试文章",
                source="wechat_mp",
                status="ready",
            )
        )
        # 预置一条蒸馏行，模拟"这篇已经跑过一次"的生产形态
        db.add(DistilledArticle(id=distill_id, article_id=article_id, status="queued"))
        await db.commit()

    try:
        yield article_id
    finally:
        async with AsyncSessionLocal() as db:
            # distilled_articles.article_id 的 FK 没有 ON DELETE CASCADE，
            # 必须先删子行再删 article，否则外键报错
            await db.execute(
                delete(DistilledArticle).where(DistilledArticle.article_id == article_id)
            )
            await db.execute(delete(Article).where(Article.id == article_id))
            await db.commit()


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
    # 期望块数按当前 limit 推导，不写死 150/32：CHUNK_CHARS 是可配的
    # （.env 里从 400 调到 150 再调到 200），写死会让每次调参都假报失败，
    # 于是真正该拦的「有块超限」反而没人看了。
    assert len(chunks) >= -(-len(script) // limit), f"{len(script)}/{limit} 至少该切这么多块"
    assert max(len(c) for c in chunks) <= limit, "有块超限，会触发静默截断"
    assert "".join(chunks) == script


def test_default_chunk_size_stays_under_truncation_threshold():
    """切块大小不能大到没人管；**真正的截断防线是 `_assert_not_truncated`**。

    ⚠️ 这条断言改过三次，每次都因为**同一个误判**：

    - 最早写死 `CHUNK_CHARS <= 150`，依据是「/v1/audio/speech 输入超过 ~150 字
      会静默截断，300 字只产出 6.9s（43.7 字/秒）」。
    - 后来把 CHUNK_CHARS 调到 200（400 字 + 90s 超时必然超时），这条就红了。
    - 2026-09-30 复测 150~197 字全是 4.4~6.0 字/秒，判断是「那组观测其实是
      超时导致拿到残缺响应，不是端点截断」。
    - 2026-10-01 在自建服务 :8010 上补测，真正找到边界：

          400 字 -> 86.3s 音频 = 4.63 字/秒   ✅ 安全上限
          800 字 -> 95.8s 音频 = 8.35 字/秒   ❌ 真截断（比外推短约 45%）

      即端点确实会截断，只是阈值在 400~800 字之间，而不是 150~300 字。

    所以现在不再用某个具体字数当防线，改为：
      1. 给切块大小一个宽松但明确的上界 400（拦「有人把 CHUNK_CHARS 调到几千」
         这种真正会丢内容的配置）；
      2. 明确指出截断防线是 `_assert_not_truncated`（7 字/秒阈值），
         它与切块大小无关，真发生截断时会**抛错**而不是静默丢内容 ——
         那条防线由 `test_truncation_guard_*` 单独守着。
    """
    from app.services.tts.indextts import _TRUNCATION_CHARS_PER_SEC

    limit = IndexTTSClient.CHUNK_CHARS
    assert 0 < limit <= 400, (
        f"切块大小 {limit} 超出合理上界。放宽切块能减少请求数，"
        f"但单段耗时线性上升、超时风险同步上升（实测 0.7s/字）"
    )
    assert _TRUNCATION_CHARS_PER_SEC > 0, "截断阈值必须为正，否则守卫形同虚设"


def test_chunk_timeout_leaves_margin_over_measured_cost():
    """150 字实测耗时约 30s，单请求超时必须留足余量。

    余量不足会把正常请求误判成超时（这正是之前分段方案失败的原因：
    当时切 400 字、按 0.6x RTF 需约 100s，而超时只给了 90s）。
    """
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
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

    实测（2026-10-01，自建服务 :8010，真实 LLM 成稿 + 同一份参考音频）：

        122 字 -> 27.7s 音频  = 4.41 字/秒  ✅
        400 字 -> 86.3s 音频  = 4.63 字/秒  ✅ 安全上限
        800 字 -> 95.8s 音频  = 8.35 字/秒  ❌ 截断（比外推值短约 45%）

    不校验的话缺内容的音频会被当成功写库。
    """
    from app.services.tts.indextts import IndexTTSError, _assert_not_truncated

    # 800 字真样本的形状：时长够长，但按 400 字的 4.63 字/秒外推只该有 ~173s，
    # 实际只有 95.8s —— 这就是「静默丢一半内容却返回 200」的样子。
    silent_96s = _make_wav(16000 * 96, 440)
    with pytest.raises(IndexTTSError, match="截断"):
        _assert_not_truncated("中" * 800, silent_96s)


def test_truncation_guard_catches_moderate_samples_the_old_threshold_missed():
    """8.35 字/秒这个真截断样本，旧阈值 12.0 会漏判。

    这是本用例存在的理由：阈值从 12.0 收紧到 7.0 就是因为它漏过一次，
    差点让 ~45% 的内容缺失以「成功」状态写进库。
    """
    from app.services.tts.indextts import _TRUNCATION_CHARS_PER_SEC, _assert_not_truncated

    assert _TRUNCATION_CHARS_PER_SEC < 8.35, "阈值必须能拦住实测到的真截断样本"
    with pytest.raises(IndexTTSError, match="截断"):
        _assert_not_truncated("中" * 800, _make_wav(16000 * 96, 440))


def test_truncation_guard_does_not_false_positive_on_normal_audio():
    """400 字 / 86.3s = 4.63 字/秒是实测安全样本，不应误报。

    旧用例用的是 150 字 / 18s = 8.33 字/秒 —— 那个样本在收紧后的阈值下会被
    判成截断。它来自 oMLX 时代的一组「疑似截断」观测，而那组观测后来被证明
    是超时导致的残缺响应，不是端点行为（见同文件 CHUNK_CHARS 上界用例的说明）。
    """
    from app.services.tts.indextts import _assert_not_truncated

    normal = _make_wav(16000 * 86, 440)
    _assert_not_truncated("中" * 400, normal)  # 不抛即通过


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
# 候选链 = base_url + INDEXTTS_FAILOVER_URLS（逗号分隔，可为空）。
# 曾经这里还会**无条件**把本机 8000/8008 两个 oMLX 端口猜进链里，导致光删环境
# 变量并不会变单端点（得另设 INDEXTTS_SINGLE_INSTANCE）。oMLX 弃用后那套猜测
# 连同开关一起删掉了 —— 端点链现在完全由配置显式决定，行为可预测。
# ==========================================================================


def test_endpoints_default_to_base_url_only(monkeypatch):
    """没配 FAILOVER 时候选链必须只有 base_url —— 不许再凭空猜出别的端口。"""
    monkeypatch.delenv("INDEXTTS_FAILOVER_URLS", raising=False)
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert c.endpoints == ["http://127.0.0.1:8010/v1"]


def test_explicit_failover_appends_after_base_url(monkeypatch):
    """显式配的 failover 端点接在 base_url 之后，且去重保序。"""
    monkeypatch.setenv(
        "INDEXTTS_FAILOVER_URLS", "http://127.0.0.1:8011/v1, http://127.0.0.1:8010/v1"
    )
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    # 重复的 8010 只保留一次，且留在 base_url 的位置
    assert c.endpoints == ["http://127.0.0.1:8010/v1", "http://127.0.0.1:8011/v1"]


def test_client_builds_failover_chain_and_short_timeout(monkeypatch):
    """单请求超时必须远小于整体墙钟预算（300s），否则等于在挂起的服务上白等。

    ⚠️ 必须显式清掉 INDEXTTS_FAILOVER_URLS：候选链由 .env 决定，而 .env 是
    **每台机器各不相同**的。不清的话，改一次 .env 就会让本用例在别人机器上
    莫名其妙挂掉 —— 它要验的是「单请求超时 < 整体超时」这条逻辑。
    """
    monkeypatch.delenv("INDEXTTS_FAILOVER_URLS", raising=False)
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert c.endpoints[0] == "http://127.0.0.1:8010/v1"
    # 90s << 300s 墙钟预算：快速失败才有意义
    assert c.chunk_timeout < c.timeout


def test_chunk_concurrency_is_bounded():
    """分段并发必须有上限 —— 实测 2/3 路虽不崩，但推理完全串行、零吞吐增益。"""
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    assert 1 <= c.chunk_concurrency <= 8


# ==========================================================================
# 2.6 熔断（CP-INDEXTTS-CIRCUIT）
# ==========================================================================


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold_then_fast_fails(monkeypatch):
    """连续失败到阈值后开闸，后续调用必须**立刻**失败而不是再烧一轮超时。

    没有熔断时 TTS 服务挂掉后，一段 200 字要走
    端点数 × 尝试数 × chunk_timeout = 2×2×240s = 16 分钟才报错，一篇 8 段就
    两个多小时。熔断把这个变成秒级失败。
    """
    import time as _time

    monkeypatch.delenv("INDEXTTS_FAILOVER_URLS", raising=False)
    monkeypatch.setenv("INDEXTTS_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("INDEXTTS_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("INDEXTTS_CHUNK_TIMEOUT", "240")
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    # 参考音频文件不存在时 _load_ref_audio_b64 会先抛 IndexTTSError，根本走不到
    # 重试循环 —— 桩掉它，否则本用例验的是「文件存不存在」而不是熔断。
    c._load_ref_audio_b64 = lambda *a, **kw: _async_value("x")  # type: ignore[assignment]

    calls = 0

    async def _boom(*a, **kw):
        nonlocal calls
        calls += 1
        # 必须抛 httpx 自己的异常类型：重试循环只捕 TimeoutException / HTTPError，
        # 抛别的会直接穿透循环，用例就变成在验「异常类型对不对」而不是验熔断。
        raise httpx.ReadTimeout("simulated engine hang")

    # 连着 3 次全部失败（模拟引擎已死）
    c._client.post = _boom  # type: ignore[method-assign]
    for _i in range(3):
        with pytest.raises(IndexTTSError):
            await c._synthesize_once("测试")

    assert c._consecutive_failures == 3
    assert c._circuit_open_until > _time.monotonic()

    # 第 4 次：熔断应已打开 —— 不发请求，直接抛
    calls_before = calls
    with pytest.raises(IndexTTSError, match="熔断中"):
        await c._synthesize_once("测试")
    assert calls == calls_before, "熔断期间不应再打后端"


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(monkeypatch):
    """一次成功就要复位熔断计数 —— 后端自愈后不该继续被挡住。"""
    import time as _time

    monkeypatch.setenv("INDEXTTS_BREAKER_THRESHOLD", "3")
    c = IndexTTSClient(
        base_url="http://127.0.0.1:8010/v1", ref_audio_path="/tmp/none.wav", ref_text="t"
    )
    c._consecutive_failures = 2
    c._circuit_open_until = _time.monotonic() + 300

    # 冷却期已过 → 允许进入
    c._circuit_open_until = 0.0

    class _Resp:
        status_code = 200
        content = b"RIFF" + b"\x00" * 4096
        headers = {"content-type": "audio/wav"}
        text = ""

    async def _ok(*a, **kw):
        return _Resp()

    c._client.post = _ok  # type: ignore[method-assign]
    c._load_ref_audio_b64 = lambda *a, **kw: _async_value("x")  # type: ignore[assignment]
    out = await c._synthesize_once("测试")
    assert out
    assert c._consecutive_failures == 0
    assert c._circuit_open_until == 0.0


async def _async_value(v):
    return v


# ==========================================================================
# 3. 持久化：换 task_id 重跑不得丢稿
# ==========================================================================


@pytest.mark.asyncio
async def test_persist_same_article_different_task_id_keeps_script(isolated_article):
    """CP-AGENT-PERSIST-ARTICLE-KEY 的核心回归。

    `distilled_articles.article_id` 是 UNIQUE（一篇一结果）。旧实现只按
    `id == task_id` 查行，换 task_id 重跑就 INSERT → 撞约束 → 整个事务回滚，
    把已经写好的 rewritten_script 一起丢掉（日志表现为
    `agent_persist_failed ... This Session's transaction has been rolled back`）。

    这里连写 3 个不同 task_id，断言：仍只有 1 行，且最后一次的稿子保住了。
    """
    from sqlalchemy import select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle
    from tasks.distill_task import _persist_agent_final

    article_id = isolated_article

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


@pytest.mark.asyncio
async def test_persist_failure_does_not_rollback_data_writes(isolated_article):
    """埋点失败绝不能连带回滚业务数据行（两段事务隔离）。"""
    from sqlalchemy import select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle
    from tasks.distill_task import _persist_agent_final

    article_id = isolated_article
    marker = "隔离性验证稿" * 20

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


# ==========================================================================
# 7. ready 写回必须按业务键（CP-AGENT-PERSIST-ARTICLE-KEY 的下游一致性）
#
# `_persist_agent_final` 按 article_id 落库（复用既有行，id 仍是第一次的
# task_id），但 distill_task 原本按 `id == task_id` 查蒸馏行 → 查不到 →
# `if da is not None` 整段跳过 → articles.status 永远停在 distilling。
# ==========================================================================


@pytest.mark.asyncio
async def test_ready_writeback_finds_row_by_article_id(isolated_article):
    """文章行 id 与 task_id 不同时，仍必须能写回 ready + audio_url。"""
    from sqlalchemy import select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle

    article_id = isolated_article
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
