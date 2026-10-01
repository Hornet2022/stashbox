"""agent_hook_bridge 的单元测试（2026-10-02）。

这个模块是 CP3.7.2 hook 体系接回生产活路径的唯一通道，逻辑不复杂但错了很隐蔽：
_bridging 写错时 hook 会「跑了但什么都没做」，和没接一样。
"""

from __future__ import annotations

import pytest

from distill.agent_hook_bridge import (
    _split_script,
    build_ctx_from_agent_final,
    run_post_hooks,
    run_pre_hooks,
)


class TestSplitScript:
    def test_empty_script_gives_empty_rewrite(self):
        rw = _split_script("")
        assert rw.hook == ""
        assert rw.sections == []
        assert rw.word_count == 0

    def test_single_paragraph_becomes_hook_truncated(self):
        rw = _split_script("很长的一段话" * 50)
        # 单段稿整体当 hook，且按 80 字上限截断 —— 池子里的样本必须是短钩子
        assert len(rw.hook) == 80
        assert rw.sections == []

    def test_multi_paragraph_splits_hook_sections_outro(self):
        script = "开场白。\n\n第一节内容。\n\n第二节内容。\n\n收尾。"
        rw = _split_script(script)
        assert rw.hook == "开场白。"
        assert rw.outro == "收尾。"
        assert rw.sections == ["第一节内容。", "第二节内容。"]

    def test_hook_truncated_to_80_chars(self):
        rw = _split_script("啊" * 200 + "\n\n正文\n\n结尾")
        assert len(rw.hook) == 80

    def test_word_count_is_full_script_length(self):
        script = "a\n\nb\n\nc"
        assert _split_script(script).word_count == len(script)


class TestBuildCtx:
    def test_maps_agent_final_into_ctx(self):
        ctx = build_ctx_from_agent_final(
            task_id="t1",
            article_id="art1",
            user_id=7,
            url="https://example.com",
            raw_content="原文",
            final={
                "rewritten_script": "开场\n\n正文节拍\n\n结尾",
                "final_audio_url": "https://cdn/a.m4a",
                "tts_duration_sec": 42,
            },
        )
        assert ctx.task_id == "t1"
        assert ctx.article_id == "art1"
        assert ctx.user_id == 7
        assert ctx.rewrite.hook == "开场"
        assert ctx.final is not None
        assert ctx.final.duration_sec == 42

    def test_missing_audio_leaves_final_none(self):
        ctx = build_ctx_from_agent_final(
            task_id="t1",
            article_id="art1",
            user_id=7,
            url="u",
            raw_content="",
            final={"rewritten_script": "x"},
        )
        assert ctx.final is None

    def test_tts_audio_url_used_as_fallback(self):
        ctx = build_ctx_from_agent_final(
            task_id="t",
            article_id="a",
            user_id=1,
            url="u",
            raw_content="",
            final={"tts_audio_url": "https://cdn/tts.m4a", "tts_duration_sec": 9},
        )
        assert ctx.final is not None
        assert ctx.final.audio_url == "https://cdn/tts.m4a"


class TestHookRunner:
    """hook 失败绝不能中断 —— 产物已经落库了。"""

    async def test_pre_hook_exception_is_swallowed(self, monkeypatch):
        import distill.agent_hook_bridge as bridge

        class _Boom:
            async def __call__(self, ctx, db):
                raise RuntimeError("hook 炸了")

        monkeypatch.setattr(bridge, "default_pre_hooks", lambda: [_Boom()])
        ctx = build_ctx_from_agent_final(
            task_id="t", article_id="a", user_id=1, url="u", raw_content="", final={}
        )
        # 不抛异常即为通过
        await run_pre_hooks(ctx, db=None)
        assert ctx is not None

    async def test_post_hook_exception_is_swallowed(self, monkeypatch):
        import distill.agent_hook_bridge as bridge

        ran: list[str] = []

        class _Boom:
            async def __call__(self, ctx, db):
                raise RuntimeError("hook 炸了")

        class _Ok:
            async def __call__(self, ctx, db):
                ran.append("ok")

        monkeypatch.setattr(bridge, "default_post_hooks", lambda: [_Boom(), _Ok()])
        ctx = build_ctx_from_agent_final(
            task_id="t", article_id="a", user_id=1, url="u", raw_content="", final={}
        )
        await run_post_hooks(ctx, db=None)
        # 前一个炸了，后一个照跑
        assert ran == ["ok"]


@pytest.mark.parametrize(
    "n_paragraphs,expect_sections",
    [(1, 0), (2, 1), (3, 1), (4, 2)],
)
def test_section_count_scales_with_paragraphs(n_paragraphs, expect_sections):
    script = "\n\n".join(f"第{i}段" for i in range(n_paragraphs))
    rw = _split_script(script)
    assert len(rw.sections) == expect_sections
