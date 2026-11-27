"""CP-TTS-PRESET（迁移 0042）：声明式预设音色 = 固定说话人。

## 这组测试守的是什么

问题一「分段换人感」的根因是**零样本克隆逐请求采样说话人**（≈12% 接缝换人）。
根治 = 改用上游**预设音色**（固定说话人，跨请求身份稳定，2026-11-25 生产实证：
8 块整段 28 对 min 0.8629，接缝 ±4s 全 ≥0.8519）。

设计是**声明式**：音色库某行声明 `preset_voice='uncle_fu'` ⇒ 蒸馏解析到它就走预设
路径（synthesize 只带 voice、**不带 ref_audio/ref_text**）；没声明 ⇒ 走克隆（既有行为）。
不用全局开关，因为那样**溯源诚实无法闭合**（见迁移 0042 说明）。

这里钉死四件事（每条都打在**判据所属的那一层**）：

1. **预设路径只带 voice、不带 ref**（tools.py 调用层）—— 带了 ref 就退回克隆采样，
   根治失效。断言的是 tools **真的怎么调** synthesize，不是「解析出了什么」。
2. **预设溯源诚实**（tools.py 返回层）—— 预设真被使用 ⇒ applied=True ⇒ 记 voice_id；
   与克隆路径「覆盖真生效才记」同口径（BUG#8 的延伸）。
3. **非 openai provider 声明预设 ⇒ 回落克隆**（tools.py 分支层）—— 预设是托管端点
   的能力，indextts 的音色由参考音决定（voice 无意义），不能误走预设。
4. **克隆音色（无 preset_voice）路径不变**（tools.py 分支层）—— 既有行为零回归。

service 层另钉：resolve 带上 preset_voice；create/update 的三态语义；长度校验。

前置：本机 PG + Redis 已起，且已 `alembic upgrade head`（含 0042）。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import TTSVoice, User, UserTTSPreference
from stashbox.backend.common.tts_voice_service import (
    VoiceError,
    create_voice,
    get_voice,
    resolve_voice_for_user,
    set_user_preference,
    update_voice,
)

RIFF = b"RIFF" + b"\0" * 4000


class CapturingOpenAIClient:
    """openai 风格 client：记录 synthesize 实际收到的 voice/ref_audio/ref_text。

    断言打在**调用点**（tools 真的传了什么），不是 helper —— 只测 helper 的守卫
    对「调用点忘传/多传参数」完全免疫（见仓内守卫纪律）。
    """

    provider_name = "openai"
    voice = "uncle_fu"
    last: dict | None = None

    async def synthesize(
        self, text, voice=None, ref_audio=None, ref_text=None, output_format="mp3"
    ):
        type(self).last = {"voice": voice, "ref_audio": ref_audio, "ref_text": ref_text}
        return RIFF


class CapturingIndexTTSClient:
    """indextts 风格 client：音色由参考音决定，voice 参数无意义。"""

    provider_name = "indextts"
    voice = "global-voice"
    last: dict | None = None

    async def synthesize(
        self, text, voice=None, ref_audio=None, ref_text=None, output_format="mp3"
    ):
        type(self).last = {"voice": voice, "ref_audio": ref_audio, "ref_text": ref_text}
        return RIFF


@pytest.fixture
def fake_tts(monkeypatch):
    def _install(client_cls):
        from stashbox.backend.app.services import tts as tts_pkg

        async def _reload():
            return client_cls()

        monkeypatch.setattr(tts_pkg, "reload", _reload)

    return _install


@pytest.fixture
def fake_resolve(monkeypatch):
    """把解析结果换成指定 ResolvedVoice（绕开 DB，专测 tools 分支）。"""
    from stashbox.backend.common.tts_voice_service import ResolvedVoice

    def _install(**kw):
        base = dict(
            ref_audio="/tmp/preset-ref.wav",
            ref_text="参考文本",
            voice_id="ttsv_preset_test",
            display_name="婷婷",
            source="default",
        )
        base.update(kw)
        resolved = ResolvedVoice(**base)

        async def _fake_resolve(user_id):
            return resolved

        monkeypatch.setattr(
            "stashbox.backend.common.tts_voice_service.resolve_voice_for_user",
            _fake_resolve,
        )

    return _install


async def _run_tool():
    from agent.tools import tts_synthesize_tool

    return await tts_synthesize_tool(
        {"article_id": "art_preset_test", "user_id": 0, "rewritten_script": "这是一段测试文本。"},
        {},
    )


# ==========================================================================
# 1. 预设路径只带 voice、不带 ref（tools 调用层）
# ==========================================================================


async def test_预设路径只带voice不带ref(fake_tts, fake_resolve):
    """声明了预设 ⇒ synthesize 只带 voice=预设名，**绝不带 ref_audio/ref_text**。

    带了 ref 就退回零样本克隆（逐请求采样）⇒ 根治失效。注意 resolved.ref_audio
    故意给非空值：预设路径必须**忽略**它，而不是「恰好 ref 为空才没带」。
    """
    fake_tts(CapturingOpenAIClient)
    fake_resolve(preset_voice="uncle_fu", ref_audio="/tmp/preset-ref.wav")

    await _run_tool()

    last = CapturingOpenAIClient.last
    assert last is not None, "synthesize 根本没被调用"
    assert last["voice"] == "uncle_fu", f"预设名没传进去：{last}"
    assert last["ref_audio"] is None, f"预设路径带了 ref_audio ⇒ 退回克隆采样：{last}"
    assert last["ref_text"] is None, f"预设路径带了 ref_text：{last}"


async def test_预设溯源诚实记录(fake_tts, fake_resolve):
    """预设真被使用 ⇒ applied=True ⇒ 记 voice_id/name/source（与克隆同口径）。"""
    fake_tts(CapturingOpenAIClient)
    fake_resolve(preset_voice="uncle_fu", voice_id="ttsv_preset_test", display_name="婷婷")

    out = await _run_tool()

    assert out["tts_voice_id"] == "ttsv_preset_test", "预设生效了却不记溯源"
    assert out["tts_voice_name"] == "婷婷"
    assert out["tts_voice_source"] == "default"
    assert out["voice"] == "uncle_fu", "返回的 provider voice 名应是预设名"


async def test_预设返回voice字段是预设名(fake_tts, fake_resolve):
    fake_tts(CapturingOpenAIClient)
    fake_resolve(preset_voice="uncle_fu")
    out = await _run_tool()
    assert out["voice"] == "uncle_fu"


# ==========================================================================
# 2. 非 openai provider 声明预设 ⇒ 回落克隆（tools 分支层）
# ==========================================================================


async def test_非openai声明预设回落克隆(fake_tts, fake_resolve):
    """indextts 的音色由参考音决定（voice 无意义）⇒ 即使声明了 preset_voice
    也走克隆（带 ref_audio），不能误走预设路径。

    变异敏感：若 use_preset 漏掉 provider 判定 ⇒ indextts 会走预设（不带 ref）
    ⇒ used_ref 为 None ⇒ 本断言红。
    """
    fake_tts(CapturingIndexTTSClient)
    fake_resolve(preset_voice="uncle_fu", ref_audio="/tmp/preset-ref.wav")

    out = await _run_tool()

    last = CapturingIndexTTSClient.last
    assert (
        last["ref_audio"] == "/tmp/preset-ref.wav"
    ), f"indextts 声明预设后应回落克隆（带 ref），实际：{last}"
    # 克隆覆盖生效（ref 非空）⇒ 溯源照常记录
    assert out["tts_voice_id"] == "ttsv_preset_test"


# ==========================================================================
# 3. 克隆音色（无 preset_voice）路径不变（tools 分支层，零回归）
# ==========================================================================


async def test_openai克隆音色无preset走克隆(fake_tts, fake_resolve):
    """openai provider + 未声明预设 ⇒ 仍走克隆（带 ref_audio），既有行为不变。

    变异敏感：若预设分支在 preset_voice 为空时也触发 ⇒ ref 为 None ⇒ 本断言红。
    """
    fake_tts(CapturingOpenAIClient)
    fake_resolve(preset_voice=None, ref_audio="/tmp/preset-ref.wav")

    await _run_tool()

    last = CapturingOpenAIClient.last
    assert last["ref_audio"] == "/tmp/preset-ref.wav", f"克隆音色应带 ref：{last}"
    assert last["ref_text"] == "参考文本"


async def test_openai克隆空ref溯源不记(fake_tts, fake_resolve):
    """克隆音色 ref 为空 ⇒ 克隆没发生 ⇒ 溯源记 None（BUG#8 口径，预设不该破坏它）。"""
    fake_tts(CapturingOpenAIClient)
    fake_resolve(preset_voice=None, ref_audio="")

    out = await _run_tool()

    assert out["tts_voice_id"] is None
    assert out["tts_voice_source"] == "not_applied"


# ==========================================================================
# 4. service 层：resolve 带上 preset_voice（真实 DB）
# ==========================================================================


@pytest_asyncio.fixture
async def preset_voice_row():
    """建一个声明预设的默认音色，测完删干净。"""
    voice = await create_voice(
        slug=f"preset_{uuid.uuid4().hex[:12]}",
        display_name="婷婷预设",
        ref_audio="/tmp/preset-ref.wav",
        ref_text="参考文本",
        preset_voice="uncle_fu",
        is_default=True,
    )
    yield voice.id
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserTTSPreference).where(UserTTSPreference.voice_id == voice.id))
        await db.execute(delete(TTSVoice).where(TTSVoice.id == voice.id))
        await db.commit()


async def test_resolve默认音色带上preset(preset_voice_row):
    resolved = await resolve_voice_for_user(None)
    assert resolved.voice_id == preset_voice_row
    assert resolved.preset_voice == "uncle_fu", "resolve 没把 preset_voice 带出来"
    assert resolved.source == "default"


async def test_resolve用户偏好带上preset(preset_voice_row):
    async with AsyncSessionLocal() as db:
        user = User(open_id="preset_" + uuid.uuid4().hex[:20], nickname="pytest", tier="free")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid = int(user.id)
    try:
        await set_user_preference(uid, voice_id=preset_voice_row)
        resolved = await resolve_voice_for_user(uid)
        assert resolved.preset_voice == "uncle_fu"
        assert resolved.source == "user"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(UserTTSPreference).where(UserTTSPreference.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_resolve克隆音色preset为None():
    voice = await create_voice(
        slug=f"clone_{uuid.uuid4().hex[:12]}",
        display_name="克隆音色",
        ref_audio="/tmp/clone-ref.wav",
        ref_text="参考文本",
        is_default=True,
    )
    try:
        resolved = await resolve_voice_for_user(None)
        assert resolved.voice_id == voice.id
        assert resolved.preset_voice is None, "克隆音色不该有 preset_voice"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TTSVoice).where(TTSVoice.id == voice.id))
            await db.commit()


# ==========================================================================
# 5. service 层：create/update 三态语义 + 长度校验（真实 DB）
# ==========================================================================


async def test_update_preset三态():
    voice = await create_voice(
        slug=f"tri_{uuid.uuid4().hex[:12]}",
        display_name="三态音色",
        ref_audio="/tmp/tri-ref.wav",
        ref_text="参考文本",
    )
    try:
        # 初始：克隆（preset_voice=None）
        assert (await get_voice(voice.id)).preset_voice is None
        # 传预设名 ⇒ 声明
        await update_voice(voice.id, preset_voice="uncle_fu")
        assert (await get_voice(voice.id)).preset_voice == "uncle_fu"
        # 不传（None）⇒ 不动
        await update_voice(voice.id, display_name="改名不改预设")
        assert (await get_voice(voice.id)).preset_voice == "uncle_fu"
        # 传空串 ⇒ 清空（退回克隆）
        await update_voice(voice.id, preset_voice="")
        assert (await get_voice(voice.id)).preset_voice is None
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TTSVoice).where(TTSVoice.id == voice.id))
            await db.commit()


async def test_create_preset超长报错():
    with pytest.raises(VoiceError, match="预设音色名"):
        await create_voice(
            slug=f"long_{uuid.uuid4().hex[:12]}",
            display_name="超长预设",
            ref_audio="/tmp/long-ref.wav",
            ref_text="参考文本",
            preset_voice="x" * 65,
        )


async def test_create_preset空串归一为None():
    voice = await create_voice(
        slug=f"empty_{uuid.uuid4().hex[:12]}",
        display_name="空串预设",
        ref_audio="/tmp/empty-ref.wav",
        ref_text="参考文本",
        preset_voice="   ",
    )
    try:
        assert (await get_voice(voice.id)).preset_voice is None
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TTSVoice).where(TTSVoice.id == voice.id))
            await db.commit()
