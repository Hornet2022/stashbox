"""CP-TTS-VOICE BUG#8：音色覆盖没生效时，溯源不许记成「生效了」。

**bug**：`tts_synthesize_tool` 解析出用户选的音色后，把 `ref_audio`/`ref_text`
当覆盖参数传给 `client.synthesize()`。但只有 IndexTTS 的 `synthesize` 有这两个
形参，edge/openai/doubao/local 一律 TypeError，然后代码回退到
`synthesize(script, voice=client.voice)` —— **实际用的是 provider 自己的默认音色**。

而返回 dict 里 `tts_voice_id` 照抄 `resolved.voice_id`，于是
`distilled_articles.tts_voice_id` 记下了一个**根本没参与这次合成**的音色。
实测（provider=edge）：音频是 `zh-CN-XiaoxiaoNeural` 念的，溯源却写着用户选的音色。

这为什么严重：溯源字段唯一的展示位就是详情页的「本期由 X 朗读」，
而那条文案正是「换音色 → 重新生成」闭环的验收凭据 —— 它在说谎，
用户重生成完看到「还是婷婷」，会以为重生成根本没生效。

修法：覆盖是否真的生效，由「实际走了哪条调用路径」推导（`applied` 标志），
不能由「解析出了什么」推导。覆盖没生效 → `tts_voice_id=None`、
`source='not_applied'`，与 global_config 同义：来源不可溯源。

前置：本机 PG + Redis 已起，且已 `alembic upgrade head`（含 0033）。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import (
    Article,
    TTSVoice,
    User,
    UserTTSPreference,
)
from stashbox.backend.common.tts_voice_service import create_voice, set_user_preference

RIFF = b"RIFF" + b"\0" * 4000  # 够 tools 的长度/格式校验


class OverrideCapableClient:
    """IndexTTS 风格的 client：synthesize 接受 ref_audio/ref_text。"""

    provider_name = "indextts"
    voice = "global-voice"
    used_ref: str | None = None

    async def synthesize(
        self, text, voice=None, ref_audio=None, ref_text=None, output_format="mp3"
    ):
        type(self).used_ref = ref_audio
        return RIFF


class PlainClient:
    """edge/openai/doubao/local 风格的 client：**没有** ref_audio 形参。"""

    provider_name = "edge"
    voice = "zh-CN-XiaoxiaoNeural"

    async def synthesize(self, text, voice="zh-CN-XiaoxiaoNeural", output_format="mp3"):
        return RIFF


@pytest_asyncio.fixture
async def user_with_voice():
    """用户 + 选好的音色，测完整条链删干净。"""
    async with AsyncSessionLocal() as db:
        user = User(open_id="cp_bug8_" + uuid.uuid4().hex[:20], nickname="pytest", tier="free")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid = int(user.id)

    voice = await create_voice(
        slug=f"bug8_{uuid.uuid4().hex[:12]}",
        display_name="婷婷假音色",
        ref_audio="/tmp/bug8-ref.wav",
        ref_text="参考文本",
    )
    await set_user_preference(uid, voice_id=voice.id)

    yield uid, voice.id

    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserTTSPreference).where(UserTTSPreference.user_id == uid))
        await db.execute(delete(Article).where(Article.user_id == uid))
        await db.execute(delete(TTSVoice).where(TTSVoice.id == voice.id))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()


@pytest.fixture
def fake_tts(monkeypatch):
    """把 `app.services.tts.reload` 换成指定 client。"""

    def _install(client_cls):
        from stashbox.backend.app.services import tts as tts_pkg

        async def _reload():
            return client_cls()

        monkeypatch.setattr(tts_pkg, "reload", _reload)

    return _install


async def test_provider不支持覆盖时溯源不记音色(user_with_voice, fake_tts):
    """核心回归：edge 风格 provider 下，tts_voice_id 必须是 None。"""
    from agent.tools import tts_synthesize_tool

    uid, voice_id = user_with_voice
    fake_tts(PlainClient)

    out = await tts_synthesize_tool(
        {"article_id": "art_bug8_plain", "user_id": uid, "rewritten_script": "这是一段测试文本。"},
        {},
    )

    assert out["tts_voice_id"] is None, (
        f"provider=edge 时音色覆盖根本不会生效（实际用的是 "
        f"{PlainClient.voice}），却把用户选的音色记成了溯源 —— "
        f"详情页会显示「本期由 婷婷假音色 朗读」，在说谎"
    )
    assert out["tts_voice_source"] == "not_applied"
    assert out["tts_voice_name"] is None


async def test_provider不支持覆盖时任务仍要成功(user_with_voice, fake_tts):
    """不能因为「用不了音色」就让整篇蒸馏挂掉 —— 退回默认音色是刻意的兜底。"""
    from agent.tools import tts_synthesize_tool

    uid, _ = user_with_voice
    fake_tts(PlainClient)

    out = await tts_synthesize_tool(
        {"article_id": "art_bug8_plain2", "user_id": uid, "rewritten_script": "这是一段测试文本。"},
        {},
    )

    assert out["bytes_len"] > 100
    assert out["audio_path"]


async def test_indextts下溯源照常记录(user_with_voice, fake_tts):
    """反向验证：覆盖**生效**时必须照常记录，别把好的一起修没了。"""
    from agent.tools import tts_synthesize_tool

    uid, voice_id = user_with_voice
    fake_tts(OverrideCapableClient)

    out = await tts_synthesize_tool(
        {
            "article_id": "art_bug8_capable",
            "user_id": uid,
            "rewritten_script": "这是一段测试文本。",
        },
        {},
    )

    assert OverrideCapableClient.used_ref == "/tmp/bug8-ref.wav", "覆盖参数没传进去"
    assert out["tts_voice_id"] == voice_id
    assert out["tts_voice_source"] == "user"
    assert out["tts_voice_name"] == "婷婷假音色"
