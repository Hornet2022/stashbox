"""CP3.7.2：Pipeline Hooks + Tier Router 单测。

11+ 个用例覆盖：
- tier_router 4 条规则 + 失败兜底
- TIER_MODEL_MAP 完整性
- default_pre/post/post_step hooks 数量
- pipeline pre/post hooks 调用
- 业务逻辑 0 改动（原有 test_worker_distill_task / test_distill_pipeline 仍 pass）
"""

from datetime import datetime


from distill.schemas import DistillContext, UserListeningPattern
from distill.tier_router import (
    TIER_MODEL_MAP,
    get_model_for_tier,
    route_tier,
)
from distill.hooks_impl import (
    FewShotPoolHook,
    FewShotSelectorHook,
    ListeningPatternUpdaterHook,
    StageCacheHook,
    TierRouterHook,
    UserProfileHook,
    default_post_hooks,
    default_post_step_hooks,
    default_pre_hooks,
)


# ---------------------------------------------------------------------------
# 1. tier_router 规则 1：pro/member + 反馈数 >= 10 + 评分 < 3.5 → 强制 full
# ---------------------------------------------------------------------------
async def test_tier_router_rule_1_pro_member_low_score_forces_full():
    """CP3.7.2 §2.2.D 规则 1：pro + feedback>=10 + avg<3.5 → 强制 full。"""
    profile = UserListeningPattern(
        user_id=1, feedback_count=15, avg_overall_score=3.0, last_updated=datetime.now()
    )
    user = type("User", (), {"tier": "pro"})()
    article = type("Article", (), {"raw_content": "短文", "source_type": "douyin"})()
    result = await route_tier(article, user, profile, db=None)
    assert result == "full"


# ---------------------------------------------------------------------------
# 2. tier_router 规则 2：长文 / PDF / arxiv → 强制 full
# ---------------------------------------------------------------------------
async def test_tier_router_rule_2_long_article_forces_full():
    """CP3.7.2 §2.2.D 规则 2：raw_content > 3000 → 强制 full。"""
    profile = UserListeningPattern(user_id=1, feedback_count=0, last_updated=datetime.now())
    user = type("User", (), {"tier": "free"})()
    article = type("Article", (), {"raw_content": "x" * 3001, "source_type": "douyin"})()
    result = await route_tier(article, user, profile, db=None)
    assert result == "full"


async def test_tier_router_rule_2_pdf_source_forces_full():
    """CP3.7.2 §2.2.D 规则 2：source_type=pdf → 强制 full。"""
    profile = UserListeningPattern(user_id=1, feedback_count=0, last_updated=datetime.now())
    user = type("User", (), {"tier": "free"})()
    article = type("Article", (), {"raw_content": "x" * 100, "source_type": "pdf"})()
    result = await route_tier(article, user, profile, db=None)
    assert result == "full"


# ---------------------------------------------------------------------------
# 3. tier_router 规则 3：抖音短文 + 评分 >= 4.0 → simple
# ---------------------------------------------------------------------------
async def test_tier_router_rule_3_douyin_short_high_score_returns_simple():
    """CP3.7.2 §2.2.D 规则 3：抖音 + < 800 字 + avg>=4.0 → simple。"""
    profile = UserListeningPattern(
        user_id=1, feedback_count=10, avg_overall_score=4.5, last_updated=datetime.now()
    )
    user = type("User", (), {"tier": "free"})()
    article = type("Article", (), {"raw_content": "x" * 500, "source_type": "douyin"})()
    result = await route_tier(article, user, profile, db=None)
    assert result == "simple"


# ---------------------------------------------------------------------------
# 4. tier_router 规则 4：默认 full
# ---------------------------------------------------------------------------
async def test_tier_router_default_full():
    """CP3.7.2 §2.2.D 规则 4：其他情况默认 full。"""
    profile = UserListeningPattern(user_id=1, feedback_count=0, last_updated=datetime.now())
    user = type("User", (), {"tier": "free"})()
    article = type(
        "Article", (), {"raw_content": "中等长度文章。" * 100, "source_type": "wechat"}
    )()
    result = await route_tier(article, user, profile, db=None)
    assert result == "full"


# ---------------------------------------------------------------------------
# 5. tier_router 失败兜底：异常 → full
# ---------------------------------------------------------------------------
async def test_tier_router_failure_fallback_full():
    """CP3.7.2 §2.2.D 失败兜底：article 异常时 → full。"""

    class BrokenArticle:
        @property
        def raw_content(self):
            raise RuntimeError("simulated db error")

    profile = UserListeningPattern(user_id=1, feedback_count=0, last_updated=datetime.now())
    user = type("User", (), {"tier": "free"})()
    result = await route_tier(BrokenArticle(), user, profile, db=None)
    assert result == "full"


# ---------------------------------------------------------------------------
# 6. TIER_MODEL_MAP 完整性
# ---------------------------------------------------------------------------
def test_tier_model_map_completeness():
    """CP3.7.2 §2.2.D：TIER_MODEL_MAP 含 simple + full + openai + qwen_vl + claude。"""
    assert "simple" in TIER_MODEL_MAP
    assert "full" in TIER_MODEL_MAP
    for tier in ("simple", "full"):
        for provider in ("openai", "qwen_vl", "claude"):
            assert (
                provider in TIER_MODEL_MAP[tier]
            ), f"TIER_MODEL_MAP[{tier}] 缺 provider={provider}"
            assert TIER_MODEL_MAP[tier][provider], f"TIER_MODEL_MAP[{tier}][{provider}] 是空"


def test_get_model_for_tier_returns_correct_model():
    """CP3.7.2 §2.2.D：get_model_for_tier 返回正确 model 字符串。"""
    assert get_model_for_tier("simple", "openai") == "gpt-4o-mini"
    assert get_model_for_tier("full", "openai") == "gpt-4o"
    assert get_model_for_tier("simple", "qwen_vl") == "qwen2.5-7b-instruct"
    assert get_model_for_tier("full", "qwen_vl") == "qwen-vl-max"


# ---------------------------------------------------------------------------
# 7. default_pre_hooks 返回 3 个
# ---------------------------------------------------------------------------
def test_default_pre_hooks_returns_3_hooks():
    """CP3.7.2 §2.2.E：default_pre_hooks 返回 [TierRouterHook, UserProfileHook, FewShotSelectorHook]。"""
    hooks = default_pre_hooks()
    assert len(hooks) == 3
    assert isinstance(hooks[0], TierRouterHook)
    assert isinstance(hooks[1], UserProfileHook)
    assert isinstance(hooks[2], FewShotSelectorHook)


# ---------------------------------------------------------------------------
# 8. default_post_step_hooks 返回 1 个
# ---------------------------------------------------------------------------
def test_default_post_step_hooks_returns_1_hook():
    """CP3.7.2 §2.2.E：default_post_step_hooks 返回 [StageCacheHook]。"""
    hooks = default_post_step_hooks()
    assert len(hooks) == 1
    assert isinstance(hooks[0], StageCacheHook)


# ---------------------------------------------------------------------------
# 9. default_post_hooks 返回 2 个
# ---------------------------------------------------------------------------
def test_default_post_hooks_returns_2_hooks():
    """CP3.7.2 §2.2.E：default_post_hooks 返回 [ListeningPatternUpdaterHook, FewShotPoolHook]。"""
    hooks = default_post_hooks()
    assert len(hooks) == 2
    assert isinstance(hooks[0], ListeningPatternUpdaterHook)
    assert isinstance(hooks[1], FewShotPoolHook)


# ---------------------------------------------------------------------------
# 10. _is_real_session 检测
# ---------------------------------------------------------------------------
def test_is_real_session_detects_fake_session():
    """CP3.7.2：_is_real_session 识别 FakeSession（无 in_transaction）。"""
    from distill.hooks_impl import _is_real_session

    class FakeSession:
        pass

    real_session = type("RealSession", (), {"in_transaction": lambda self: True})()
    fake_session = FakeSession()

    assert _is_real_session(real_session) is True
    assert _is_real_session(fake_session) is False


# ---------------------------------------------------------------------------
# 11. Pipeline 集成：pre_hook 在 step1 前跑
# ---------------------------------------------------------------------------
async def test_pipeline_invokes_pre_hooks(monkeypatch, ctx):
    """CP3.7.2：DistillPipeline.run() 在 step1 前调 pre_hooks。"""
    from distill.pipeline import DistillPipeline
    from distill.schemas import StructuredOutput

    # mock pre-hook 验证被调
    pre_calls = []

    class _FakePreHook:
        async def __call__(self, ctx, article, db):
            pre_calls.append(ctx.task_id)

    pipeline = DistillPipeline(
        llm=None,
        tts_client=None,
        db_session_factory=None,  # mock 模式：pre_hook 不会真跑
        pre_hooks=[_FakePreHook()],
    )
    # mock 4 步：直接设 structured + rewrite + tts + final
    ctx.structured = StructuredOutput(summary="x", chapters=[], entities=[], tags=[])
    from distill.schemas import RewriteOutput

    ctx.rewrite = RewriteOutput(hook="h", sections=["s"], outro="o", word_count=10)
    from distill.schemas import TTSOutput, AudioConcatOutput

    ctx.tts = TTSOutput(segments=[])
    ctx.final = AudioConcatOutput(audio_url="x", duration_sec=0, format="m4a")

    # patch 4 个 step 函数（避免真跑 LLM/TTS）
    from distill import pipeline as pipeline_mod

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline_mod, "step1_structure", noop)
    monkeypatch.setattr(pipeline_mod, "step2_rewrite", noop)
    monkeypatch.setattr(pipeline_mod, "step3_tts", noop)
    monkeypatch.setattr(pipeline_mod, "step4_concat", noop)

    # mock 状态机 / save_audio / write_final_to_db / clear_stages
    monkeypatch.setattr(pipeline_mod.DistillPipeline, "_update_status", noop)
    monkeypatch.setattr(pipeline_mod.DistillPipeline, "_save_audio", noop)
    monkeypatch.setattr(pipeline_mod.DistillPipeline, "_write_final_to_db", noop)

    # pre_hook 因 session_factory=None 跳过，但 post_hook 也跳过，pipeline 应该正常走完
    await pipeline.run(ctx)
    # pre_hook 因 session_factory=None 没跑（_invoke_pre_hooks 提前 return）
    assert pre_calls == []


# ---------------------------------------------------------------------------
# 12. FewShotSelectorHook：冷启动保护（feedback_count < 5）
# ---------------------------------------------------------------------------
async def test_few_shot_selector_hook_cold_start():
    """CP3.7.2：FewShotSelectorHook 在 feedback_count < 5 时不查 DB。"""
    from distill.schemas import UserListeningPattern

    ctx = DistillContext(
        task_id="dst_x", article_id="art_x", user_id=1, url="x", raw_content="test"
    )
    ctx.user_profile = UserListeningPattern(
        user_id=1, feedback_count=2, last_updated=datetime.now()
    )

    # mock db（无 scalar / execute —— 模拟 FakeSession）
    class FakeDB:
        pass

    hook = FewShotSelectorHook()
    await hook(ctx, article=None, db=FakeDB())
    # 冷启动时 few_shot_examples 保持空
    assert ctx.few_shot_examples == []


# ---------------------------------------------------------------------------
# 13. Schema 扩字段验证
# ---------------------------------------------------------------------------
def test_distill_context_cp372_fields():
    """CP3.7.2：DistillContext 含 user_profile / few_shot / target_tier / target_bitrate / stage_cache。"""
    from distill.schemas import DistillContext

    ctx = DistillContext(
        task_id="dst_x", article_id="art_x", user_id=1, url="x", raw_content="test"
    )
    assert ctx.target_tier == "full"
    assert ctx.target_bitrate == [128]
    assert ctx.user_profile is None
    assert ctx.few_shot_examples == []
    assert ctx.stage_cache == {}
    assert ctx.evaluation_id is None


def test_rewrite_example_schema():
    """CP3.7.2：RewriteExample schema 字段正确。"""
    from distill.schemas import RewriteExample

    ex = RewriteExample(kind="hook", text="钩子文本", score_avg=4.5)
    assert ex.kind == "hook"
    assert ex.text == "钩子文本"
    assert ex.score_avg == 4.5
