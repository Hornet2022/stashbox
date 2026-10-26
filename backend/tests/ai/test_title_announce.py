"""CP-TITLE-ANNOUNCE（2026-10-26）：音频开头先播报本期标题。

## 需求

内测反馈：「音频在播放内容前，能不能先说明一下内容的标题，现在直接开始读正文，
播放列表的内容切换很突兀」。

## 这个特性真正要防住的三件事

1. **听的人不知道自己在听什么** —— 播报句必须真的进音频。
2. **音画不同源** —— App 的字幕时间轴是从 script_text 按字数比例估算的
   （生产的 TTS 不返回词级时间戳）。音频多播 4 秒而 script_text 没有对应的字
   ⇒ 字幕整体早 4 秒，误差随进度线性放大。⇒ 播报句必须同时进 script_text。
3. **few-shot 池被污染** —— 入池取的是「稿子第一段」（当 hook 用）。
   播报句占了第一段 ⇒ 所有后续改写都学会用同一句开场（公式化开场），
   而这个污染**完全静默**。⇒ 真正的 hook 必须由服务端**明确**交出来
   （content_parts.hook），入池改读它。

## 变异检验（这个文件的核心验收方式）

护栏类测试只有在「抽掉这条检查 ⇒ 对应用例变红」时才算有效。
逐条抽（不是抽一条代表性的）：

- 抽掉 `evaluation_service` 里读 content_parts.hook 的分支
  → `test_入池取的是真_hook_不是播报句` 变红；
- 抽掉 `runner.py` 里 prepend 那一步
  → `test_整稿第一段是播报句` 变红；
- 抽掉 `steps.py` 里 insert 播报段那一步
  → `test_旧路径_第一段是播报句` 变红；
- 抽掉标题清洗里的书名号 / 换行 / 下划线规则
  → 对应的清洗用例变红。
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

AI_SERVICE_DIR = str(Path(__file__).resolve().parents[2] / "ai-service")
if AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, AI_SERVICE_DIR)


# ---------------------------------------------------------------------------
# 1. 标题清洗（每条规则都对应一个具体的失败）
# ---------------------------------------------------------------------------


def test_换行变成空格():
    """换行会让 TTS 的切块把标题劈成两半（听感上是念到一半断了）。"""
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    assert sanitize_title_for_speech("第一行\n第二行") == "第一行 第二行"


def test_书名号被去掉():
    """TTS 对书名号的处理不一致，常见结果是**吞字**（整段标题读不出来）。"""
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    assert sanitize_title_for_speech("《深度解析》") == "深度解析"
    assert "《" not in sanitize_title_for_speech("《深度解析》")


def test_引号被去掉():
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    assert sanitize_title_for_speech('"Gemini 4 研发"') == "Gemini 4 研发"


def test_标题内部的句号变成空格():
    """标题里的句号会让切块把标题从中间切开。"""
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    out = sanitize_title_for_speech("AI。时代的三个变量")
    assert "。" not in out
    assert out == "AI 时代的三个变量"


def test_下划线变成空格():
    """文件名型标题的下划线会被念成"下划线"或直接吞掉。"""
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    out = sanitize_title_for_speech("reference-vs-rag_深度调研_v1.0")
    assert "_" not in out
    assert out == "reference-vs-rag 深度调研 v1.0"


def test_连续空白被压成一个():
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    assert sanitize_title_for_speech("标题   里　有  空白") == "标题 里 有 空白"


def test_超长标题被截断():
    """播报是导览不是朗读全文 —— 太长的标题会让人等不及正文。"""
    from stashbox.backend.common.title_announce import MAX_TITLE_CHARS, sanitize_title_for_speech

    long_title = "标" * 80
    out = sanitize_title_for_speech(long_title)
    assert len(out) == MAX_TITLE_CHARS + 1  # 40 + 省略号
    assert out.endswith("…")


def test_清洗后为空返回空串():
    from stashbox.backend.common.title_announce import sanitize_title_for_speech

    assert sanitize_title_for_speech("《》") == ""
    assert sanitize_title_for_speech("   ") == ""
    assert sanitize_title_for_speech(None) == ""


# ---------------------------------------------------------------------------
# 2. 播报句 + 开关
# ---------------------------------------------------------------------------


def test_播报句包含标题():
    from stashbox.backend.common.title_announce import build_title_announcement

    out = build_title_announcement("为什么企业 AI 越来越需要 Ontology")
    assert out is not None
    assert "为什么企业 AI 越来越需要 Ontology" in out
    assert out.endswith("。")


def test_没有标题就不播报():
    """返回 None（不是空串）—— 调用方据此判断「要不要插这一段」。"""
    from stashbox.backend.common.title_announce import build_title_announcement

    assert build_title_announcement(None) is None
    assert build_title_announcement("") is None
    assert build_title_announcement("《》") is None


def test_开关关掉就不播报(monkeypatch):
    from stashbox.backend.common.title_announce import build_title_announcement

    monkeypatch.setenv("TTS_TITLE_ANNOUNCE", "0")
    assert build_title_announcement("标题") is None

    monkeypatch.setenv("TTS_TITLE_ANNOUNCE", "false")
    assert build_title_announcement("标题") is None

    monkeypatch.setenv("TTS_TITLE_ANNOUNCE", "1")
    assert build_title_announcement("标题") is not None


def test_开关默认是开的(monkeypatch):
    """默认关的开关 = 永远不会启用。所以默认开。"""
    from stashbox.backend.common.title_announce import build_title_announcement

    monkeypatch.delenv("TTS_TITLE_ANNOUNCE", raising=False)
    assert build_title_announcement("标题") is not None


def test_播报句是独立的一段():
    """段落边界必须是 \\n\\n —— 全仓「一段 = 一段」的约定（字幕分段 / 入池取首段都按它切）。"""
    from stashbox.backend.common.title_announce import prepend_title_announcement

    script, announcement = prepend_title_announcement("钩子\n\n正文", "标题")
    assert announcement is not None
    assert script.split("\n\n")[0] == announcement
    assert script == f"{announcement}\n\n钩子\n\n正文"


def test_没有标题时稿子原样返回():
    from stashbox.backend.common.title_announce import prepend_title_announcement

    script, announcement = prepend_title_announcement("钩子\n\n正文", None)
    assert script == "钩子\n\n正文"
    assert announcement is None


# ---------------------------------------------------------------------------
# 3. agent 活路径（生产路径）接线
# ---------------------------------------------------------------------------


_REWRITE_JSON = (
    '{"hook": "今天聊聊企业为什么需要本体。", '
    '"sections": ["第一段正文。", "第二段正文。"], '
    '"outro": "就聊到这。"}'
)


@pytest.fixture
def _fake_llm(monkeypatch):
    """把 LLM 调用换掉，只回一段固定 JSON。

    同时关掉「再创作内容补救」（它会再调一次 LLM，本用例与它无关）。
    """
    from agent import runner as runner_mod

    async def _fake_chat_text(llm, user_prompt, **kwargs):
        return _REWRITE_JSON

    monkeypatch.setattr(runner_mod, "_chat_text", _fake_chat_text)
    monkeypatch.setattr(runner_mod, "_REMEDIATE_ENABLED", False)


async def test_整稿第一段是播报句(_fake_llm):
    """播报句必须**独占一段**并在最前 —— 音频是从整稿第一个字开始念的。"""
    from agent.runner import rewrite_node

    result = await rewrite_node(
        {"article_id": "art_1", "title": "为什么需要 Ontology", "fetched_content": "原文正文"}
    )

    script = result["rewritten_script"]
    first = script.split("\n\n")[0]
    assert "为什么需要 Ontology" in first
    assert result["title_announcement"] == first


async def test_真正的_hook_不含播报句(_fake_llm):
    """hook 是给 few-shot 池 / 详情页导语用的，不能混进播报句。"""
    from agent.runner import rewrite_node

    result = await rewrite_node(
        {"article_id": "art_1", "title": "为什么需要 Ontology", "fetched_content": "原文正文"}
    )

    assert result["rewrite_hook"] == "今天聊聊企业为什么需要本体。"
    assert "本期分享的标题是" not in result["rewrite_hook"]


async def test_agent_rewrite_没有标题就不播报(_fake_llm):
    """agent 路径：title 为空时 title_announcement 为 None、正文不被插播报句。

    ⚠️ 命名带 `agent_rewrite` 前缀，与 L134 那个测纯函数
    `build_title_announcement` 的 `test_没有标题就不播报` 区分开 ——
    pytest 里同名函数后者会覆盖前者，重名会让其中一个**静默不跑**。
    """
    from agent.runner import rewrite_node

    result = await rewrite_node(
        {"article_id": "art_1", "title": None, "fetched_content": "原文正文"}
    )

    assert result["title_announcement"] is None
    assert result["rewritten_script"].split("\n\n")[0] == "今天聊聊企业为什么需要本体。"


async def test_正文内容没有被改动(_fake_llm):
    """播报只加在前面，不能动正文一个字。"""
    from agent.runner import rewrite_node

    result = await rewrite_node(
        {"article_id": "art_1", "title": "标题", "fetched_content": "原文正文"}
    )
    body = result["rewritten_script"].split("\n\n", 1)[1]

    assert body == "今天聊聊企业为什么需要本体。\n\n第一段正文。\n\n第二段正文。\n\n就聊到这。"


def test_agent_state_声明了标题字段():
    """AgentState 少了字段 → 节点 return 的值会被 reducer 丢掉。"""
    from agent.state import AgentState

    for field in ("title", "title_announcement"):
        assert field in AgentState.__annotations__, f"AgentState 缺 {field}"


# ---------------------------------------------------------------------------
# 4. 落库契约：content_parts 必须带上 hook / outro / intro
# ---------------------------------------------------------------------------


def test_落库时写进_content_parts():
    """服务端必须**明确**把 hook 交出去（不能靠客户端猜第一段）。"""
    src = (Path(AI_SERVICE_DIR) / "tasks" / "distill_task.py").read_text(encoding="utf-8")

    assert '"hook": str(final.get("rewrite_hook") or "")' in src
    assert '"outro": str(final.get("rewrite_outro") or "")' in src
    assert '"intro": str(final.get("title_announcement") or "")' in src


# ---------------------------------------------------------------------------
# 5. few-shot 入池不被播报句污染（核心护栏）
# ---------------------------------------------------------------------------


class _FakeDA:
    def __init__(self, script_text: str, content_parts):
        self.script_text = script_text
        self.content_parts = content_parts


def test_入池取的是真_hook_不是播报句():
    from distill.evaluation_service import _extract_pool_hook

    da = _FakeDA(
        script_text="本期分享的标题是：为什么需要 Ontology。\n\n今天聊聊企业为什么需要本体。\n\n正文",
        content_parts={
            "sections": ["正文"],
            "insights": [],
            "related": [],
            "follow_ups": [],
            "hook": "今天聊聊企业为什么需要本体。",
            "outro": "就聊到这。",
            "intro": "本期分享的标题是：为什么需要 Ontology。",
        },
    )

    assert _extract_pool_hook(da) == "今天聊聊企业为什么需要本体。"


def test_存量数据回退到首段():
    """存量数据的 content_parts 没有 hook 键 ⇒ 旧取法（首段就是 hook）。"""
    from distill.evaluation_service import _extract_pool_hook

    da = _FakeDA(
        script_text="今天聊聊企业为什么需要本体。\n\n正文",
        content_parts={"sections": ["正文"], "insights": [], "related": [], "follow_ups": []},
    )

    assert _extract_pool_hook(da) == "今天聊聊企业为什么需要本体。"


def test_新数据_hook_为空时不入池_不退回拿播报句():
    """hook 故意为空（降级稿）时**不能**退回去拿播报句凑数。

    「值非空才用新取法」会把这种情况错误地退到旧取法 ⇒ 播报句入池。
    """
    from distill.evaluation_service import _extract_pool_hook

    da = _FakeDA(
        script_text="本期分享的标题是：标题。\n\n正文",
        content_parts={"hook": "", "outro": "", "intro": "本期分享的标题是：标题。"},
    )

    assert _extract_pool_hook(da) == ""


def test_入池取法与整稿拼接契约一致():
    """端到端对齐：入池取法在「有播报句的整稿」上确实拿到 hook。"""
    from agent.runner import _parse_rewrite
    from distill.evaluation_service import _extract_pool_hook

    parts = _parse_rewrite(_REWRITE_JSON)
    script, announcement = None, None
    from stashbox.backend.common.title_announce import prepend_title_announcement

    script, announcement = prepend_title_announcement(parts.script, "为什么需要 Ontology")

    da = _FakeDA(
        script_text=script,
        content_parts={"hook": parts.hook, "outro": parts.outro, "intro": announcement},
    )

    assert _extract_pool_hook(da) == parts.hook
    assert announcement not in _extract_pool_hook(da)


# ---------------------------------------------------------------------------
# 6. hook 桥接层（降级路径回退切分）
# ---------------------------------------------------------------------------


def test_桥接层回退切分不会把播报句当_hook():
    """降级稿（LLM 没给结构化字段）时，桥接层靠切分猜 hook ——
    猜之前必须先把播报句摘掉。
    """
    from distill.agent_hook_bridge import build_ctx_from_agent_final

    announcement = "本期分享的标题是：标题。"
    ctx = build_ctx_from_agent_final(
        task_id="dst_1",
        article_id="art_1",
        user_id=7,
        url="https://x.com/a",
        raw_content="原文",
        final={
            "rewritten_script": f"{announcement}\n\n真正的开场白\n\n正文段落",
            "title_announcement": announcement,
            "title": "标题",
        },
    )

    assert ctx.rewrite.hook == "真正的开场白"
    assert ctx.title == "标题"


# ---------------------------------------------------------------------------
# 7. 旧 pipeline 路径（口径必须一致）
# ---------------------------------------------------------------------------


class _RecordingTTS:
    provider_name = "recording"

    def __init__(self):
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return b"\x00" * 16


async def test_旧路径_第一段是播报句():
    """两条路径各写一句 ⇒ 口径漂移，而且漂移是静默的（少播一次标题没人会发现）。"""
    from distill.schemas import DistillContext, RewriteOutput
    from distill.steps import step3_tts

    ctx = DistillContext(
        task_id="dst_1",
        article_id="art_1",
        user_id=1,
        url="https://x.com/a",
        raw_content="原文",
        title="为什么需要 Ontology",
        rewrite=RewriteOutput(
            hook="钩子。",
            sections=["第一节。"],
            outro="结尾。",
            word_count=10,
        ),
    )

    tts = _RecordingTTS()
    await step3_tts(ctx, tts)

    assert "为什么需要 Ontology" in tts.texts[0]
    assert ctx.tts_chunks[0] == tts.texts[0]


async def test_旧路径_没有标题时不播报():
    from distill.schemas import DistillContext, RewriteOutput
    from distill.steps import step3_tts

    ctx = DistillContext(
        task_id="dst_1",
        article_id="art_1",
        user_id=1,
        url="https://x.com/a",
        raw_content="原文",
        title=None,
        rewrite=RewriteOutput(hook="钩子。", sections=["第一节。"], outro="结尾。", word_count=10),
    )

    tts = _RecordingTTS()
    await step3_tts(ctx, tts)

    assert tts.texts[0] == "钩子。"


async def test_旧路径_script_text_第一段也是播报句():
    """音频里念了什么，script_text 里就得有对应的字（字幕时间轴靠它对齐）。"""
    from distill.pipeline import DistillPipeline
    from distill.schemas import AudioConcatOutput, DistillContext, RewriteOutput

    captured: dict[str, object] = {}

    class _FakeSession:
        async def execute(self, _stmt):  # noqa: ANN001
            return None

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Factory:
        def __call__(self):
            return _FakeSession()

    ctx = DistillContext(
        task_id="dst_1",
        article_id="art_1",
        user_id=1,
        url="https://x.com/a",
        raw_content="原文",
        title="为什么需要 Ontology",
        rewrite=RewriteOutput(hook="钩子。", sections=["第一节。"], outro="结尾。", word_count=10),
    )
    ctx.final = AudioConcatOutput(audio_url="https://x/a.mp3", duration_sec=10)

    pipeline = DistillPipeline(llm=None, db_session_factory=_Factory())

    # 拦 update 的 values —— 直接看要写进库的那份 script_text
    original_execute = _FakeSession.execute

    async def _capture(self, stmt):
        try:
            params = stmt.compile().params
        except Exception:  # noqa: BLE001 — 拦不住就当没抓到
            params = {}
        if params:
            captured.update(params)
        return await original_execute(self, stmt)

    _FakeSession.execute = _capture  # type: ignore[method-assign]
    try:
        await pipeline._write_final_to_db(ctx)
    finally:
        _FakeSession.execute = original_execute  # type: ignore[method-assign]

    script_text = str(captured.get("script_text") or "")
    assert script_text.split("\n\n")[0].endswith("为什么需要 Ontology。")
    assert "钩子。" in script_text


def test_旧路径与_agent路径共用同一个播报函数():
    """两处 import 的是同一个模块 —— 口径漂移的根源就是「各写一句」。"""
    steps_src = (Path(AI_SERVICE_DIR) / "distill" / "steps.py").read_text(encoding="utf-8")
    pipeline_src = (Path(AI_SERVICE_DIR) / "distill" / "pipeline.py").read_text(encoding="utf-8")
    runner_src = (Path(AI_SERVICE_DIR) / "agent" / "runner.py").read_text(encoding="utf-8")

    for src in (steps_src, pipeline_src, runner_src):
        assert "common.title_announce" in src, "有一处没走共用的播报函数"
