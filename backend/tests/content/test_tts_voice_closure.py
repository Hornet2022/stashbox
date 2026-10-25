"""音色库闭环的回归测试（2026-10-25）。

## 这轮修的是四个「后台看起来有、实际不闭环」的点

1. **新建音色默认不生效，且界面无提示**
   新建时 `is_default` 默认 false、又没有任何用户选它 ⇒ 解析链回落到默认音色
   ⇒ 这个音色**根本不会被用上**。现在管理端算出 `is_effective`，
   列表里直接标「未启用」。
2. **换默认音色后存量音频不变，批量重跑无路可走**
   脚本默认只挑 `tts_voice_id` 为空的 ⇒ 线上存量全都有音色 ⇒ 脚本跑 0 篇。
   现在有 `POST /admin/tts/voices/{id}/re-distill`（设为默认 + 重渲存量）。
3. **「从当前配置导入」是死按钮**
   那条路径只认 `indextts_ref_audio`，而生产 provider 是 **openai**
   ⇒ 配置里根本没这个键 ⇒ 必然 400。现在两个 provider 的键都读。
4. **全局兜底读不到参考音频**
   `resolve_voice_for_user` 第 3 级回退也只读 `indextts_ref_audio`
   ⇒ 生产（provider=openai）这级回退**永远是空的**。
   现在 `openai_ref_audio` / `openai_ref_text` 也读。
"""

import uuid

import pytest

from sqlalchemy import select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, User
from stashbox.backend.common.models.tts_voice import TTSVoice, UserTTSPreference

from helpers import client, content_main, new_user

ADMIN_VOICES = "/api/v1/admin/tts/voices"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _staff_token(tier: str = "admin") -> str:
    from stashbox.backend.common.auth import create_access_token

    uid, _ = await new_user()
    return create_access_token(str(uid), extra={"tier": tier, "role": tier})


@pytest.fixture
async def no_default_voice():
    """临时清掉「全局默认音色」，让解析链能落到第 3 级（全局配置兜底）。

    测试库里有一条默认音色（婷婷），不摘掉的话第 3 级永远走不到。
    退出时恢复原状。
    """
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(TTSVoice).where(TTSVoice.is_default.is_(True)))).all()
        original = rows[0] if rows else None
        for v in rows:
            v.is_default = False
        await s.commit()
    try:
        yield original
    finally:
        async with AsyncSessionLocal() as s:
            if original is not None:
                v = await s.get(TTSVoice, original.id)
                if v is not None:
                    v.is_default = True
                    await s.commit()


async def _make_voice(*, is_default: bool = False, name: str = "音色") -> str:
    async with AsyncSessionLocal() as s:
        v = TTSVoice(
            id=f"ttsv_{uuid.uuid4().hex[:24]}",
            slug=f"slug_{uuid.uuid4().hex[:8]}",
            display_name=f"{name}_{uuid.uuid4().hex[:4]}",
            ref_audio_url=f"/voices/{uuid.uuid4().hex}.wav",
            ref_text="今天天气不错",
            is_default=False,
            is_active=True,
            sort_order=0,
        )
        s.add(v)
        await s.commit()
        if is_default:
            # 手工置默认：绕过服务层的 _apply_default（部分唯一索引要求至多一条）
            v.is_default = True
            await s.commit()
        return v.id


async def _make_article_with_audio(*, voice_id: str | None) -> tuple[str, int]:
    """造一篇「有音频」的存量文章，返回 (article_id, user_id)。"""
    async with AsyncSessionLocal() as s:
        u = User(
            open_id=f"ttsv_{uuid.uuid4().hex[:24]}",
            nickname="u_" + uuid.uuid4().hex[:6],
            tier="free",
            monthly_quota=5,
        )
        s.add(u)
        await s.flush()
        art = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=u.id,
            url="https://example.com/x",
            source="web",
            status="ready",
            favorite=False,
            skip=False,
        )
        s.add(art)
        await s.flush()
        d = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=art.id,
            status="done",
            script_text="稿子",
            audio_url="https://oss/x.mp3",
            tts_voice_id=voice_id,
        )
        s.add(d)
        await s.commit()
        return art.id, u.id


async def _prefer(voice_id: str) -> int:
    """让一个用户选中某个音色。"""
    uid, _ = await new_user()
    async with AsyncSessionLocal() as s:
        s.add(UserTTSPreference(user_id=uid, voice_id=voice_id, speed=1.0))
        await s.commit()
    return uid


# ---------------------------------------------------------------------------
# 1. 列表带「有没有人在用 / 有没有生效」
# ---------------------------------------------------------------------------


async def test_admin_list_voices_带被选用户数与生效标记():
    """非默认且无人选 ⇒ 明确标「未启用」（这正是「建了但不生效」的显式化）。"""
    token = await _staff_token()
    voice_id = await _make_voice()

    async with client(token) as c:
        r = await c.get(ADMIN_VOICES)
    assert r.status_code == 200
    items = {v["id"]: v for v in r.json()["items"]}
    assert voice_id in items
    row = items[voice_id]
    assert row["selected_users"] == 0
    assert row["is_effective"] is False


async def test_admin_list_voices_有人选过就标生效():
    token = await _staff_token()
    voice_id = await _make_voice()
    await _prefer(voice_id)

    async with client(token) as c:
        r = await c.get(ADMIN_VOICES)
    row = {v["id"]: v for v in r.json()["items"]}[voice_id]
    assert row["selected_users"] == 1
    assert row["is_effective"] is True


async def test_admin_list_voices_默认音色一定生效():
    """默认音色一定「生效」（哪怕没人选它 —— 用户「跟随默认」就是用它）。

    ⚠️ 用服务层的 `update_voice` 而不是直接改字段：DB 上有部分唯一索引
    `idx_tts_voices_single_default`（至多一条 is_default），
    直接置位会撞唯一键。
    """
    from stashbox.backend.common import tts_voice_service

    token = await _staff_token()
    voice_id = await _make_voice()
    try:
        await tts_voice_service.update_voice(voice_id, is_default=True)
        async with client(token) as c:
            r = await c.get(ADMIN_VOICES)
        row = {v["id"]: v for v in r.json()["items"]}[voice_id]
        assert row["is_default"] is True
        assert row["is_effective"] is True
    finally:
        async with AsyncSessionLocal() as s:
            v = await s.get(TTSVoice, voice_id)
            if v is not None:
                v.is_default = False
                await s.commit()


# ---------------------------------------------------------------------------
# 2. 全局兜底 + 「从当前配置导入」要认 openai 的参考音频
# ---------------------------------------------------------------------------


async def test_resolve_voice_兜底读openai参考音频(monkeypatch, no_default_voice):
    """生产的 provider 是 openai ⇒ 这级回退原先**永远是空的**。"""
    from stashbox.backend.app.services import tts as tts_pkg
    from stashbox.backend.common import tts_voice_service

    async def _fake_cfg():
        return {
            "openai_ref_audio": "/voices/tingting_ref.wav",
            "openai_ref_text": "今天天气不错",
        }

    monkeypatch.setattr(tts_pkg, "current_config", _fake_cfg)
    resolved = await tts_voice_service.resolve_voice_for_user(None)
    assert resolved.source == "global_config"
    assert resolved.ref_audio == "/voices/tingting_ref.wav"
    assert resolved.ref_text == "今天天气不错"


async def test_import_from_config_认openai参考音频(monkeypatch):
    """「从当前配置导入」在 provider=openai 时不再是死按钮。"""
    from stashbox.backend.app.services import tts as tts_pkg

    token = await _staff_token()

    # ⚠️ 参考音频路径必须**每次运行都不同**：
    # 导入端点是幂等的（同 ref_audio_url 直接返回已有行）⇒ 固定路径会让
    # 本次用例拿到**上一次运行**留下的行，于是 is_default 之类的断言
    # 会随别的用例改动而漂移。
    ref = f"/voices/from_config_ref_{uuid.uuid4().hex}.wav"

    async def _fake_cfg():
        return {
            "openai_ref_audio": ref,
            "openai_ref_text": "今天天气不错",
        }

    monkeypatch.setattr(tts_pkg, "current_config", _fake_cfg)
    async with client(token) as c:
        r = await c.post(f"{ADMIN_VOICES}/import-from-config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["voice"]["ref_audio_url"] == ref
    assert body["voice"]["is_default"] is True


async def test_import_from_config_第二次导入不撞slug(monkeypatch):
    """⚠️ slug 写死 "default" 时，第二次导入必然 400「标识已存在」
    ⇒ 导入功能第二次就废了。现在 slug 会自动去重。"""
    from stashbox.backend.app.services import tts as tts_pkg

    token = await _staff_token()

    # ⚠️ 参考音频路径要**每次调用都一样**（否则第二次导入的是另一份参考音频，
    # 幂等分支自然不会命中，测的就不是「重复导入」了）。
    ref = f"/voices/second_{uuid.uuid4().hex}.wav"

    async def _fake_cfg():
        return {
            "openai_ref_audio": ref,
            "openai_ref_text": "今天天气不错",
        }

    monkeypatch.setattr(tts_pkg, "current_config", _fake_cfg)
    async with client(token) as c:
        r1 = await c.post(f"{ADMIN_VOICES}/import-from-config")
        r2 = await c.post(f"{ADMIN_VOICES}/import-from-config")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # 同一份参考音频 ⇒ 幂等（不造重复行）
    assert r2.json()["created"] is False
    assert r2.json()["voice"]["id"] == r1.json()["voice"]["id"]


async def test_import_from_config_缺参考音频_给出可操作提示(monkeypatch):
    """没有参考音频时，提示要说清「去哪儿填」，而不是只说「没有」。"""
    from stashbox.backend.app.services import tts as tts_pkg

    token = await _staff_token()

    async def _fake_cfg():
        return {"openai_model": "Qwen3-TTS-12Hz-1.7B-CustomVoice"}

    monkeypatch.setattr(tts_pkg, "current_config", _fake_cfg)
    async with client(token) as c:
        r = await c.post(f"{ADMIN_VOICES}/import-from-config")
    assert r.status_code == 400
    assert "参考音频" in r.text
    assert "TTS 设置" in r.text


# ---------------------------------------------------------------------------
# 3. 「设为默认并重渲存量」
# ---------------------------------------------------------------------------


async def test_redistill_设为默认并把存量重新入队(monkeypatch):
    """换默认音色 ⇒ 存量文章重新入队（已生成的音频不会自动变）。"""
    from helpers import FakeAIClient

    token = await _staff_token()
    target_id = await _make_voice(name="目标音色")
    other_id = await _make_voice(name="旧音色")
    article_id, _uid = await _make_article_with_audio(voice_id=other_id)

    fake = FakeAIClient()
    monkeypatch.setattr(content_main.tts_voice_router_module, "get_ai_client", lambda: fake)

    async with client(token) as c:
        r = await c.post(
            f"{ADMIN_VOICES}/{target_id}/re-distill",
            json={"reason": "换成新音色", "article_ids": [article_id]},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 1
    assert body["queued"] == 1
    assert [i["article_id"] for i in body["items"]] == [article_id]
    assert fake.calls and fake.calls[0]["article_id"] == article_id

    # 目标音色成为默认音色
    async with AsyncSessionLocal() as s:
        target = await s.get(TTSVoice, target_id)
        assert target is not None and target.is_default is True

    # 文章回到 pending（等 worker 重新蒸馏）
    async with AsyncSessionLocal() as s:
        art = await s.get(Article, article_id)
        assert art is not None and art.status == "pending"


async def test_redistill_reason太短_400():
    token = await _staff_token()
    voice_id = await _make_voice()
    async with client(token) as c:
        r = await c.post(f"{ADMIN_VOICES}/{voice_id}/re-distill", json={"reason": "换"})
    assert r.status_code == 400


async def test_redistill_音色不存在_404():
    token = await _staff_token()
    async with client(token) as c:
        r = await c.post(
            "/api/v1/admin/tts/voices/ttsv_not_exist/re-distill",
            json={"reason": "换个音色试试"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. 批量重跑脚本的新维度（SQL 口径，不连库）
# ---------------------------------------------------------------------------


def _script_module():
    """加载 scripts/redistill_voices.py（它自己 bootstrap 包路径）。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "redistill_voices.py"
    spec = importlib.util.spec_from_file_location("_redistill_voices_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redistill_script_按目标音色筛选(monkeypatch):
    """`--to-voice` ⇒ 筛出「当前音色 ≠ 目标音色」的文章。

    没有这个维度时，换完默认音色**没有任何办法**把存量换成新音色
    （脚本默认清单只挑 `tts_voice_id` 为空的，而线上存量全都有音色）。
    """
    mod = _script_module()
    seen: list[str] = []

    def _fake_rows(sql: str):
        seen.append(sql)
        return []

    monkeypatch.setattr(mod, "_rows", _fake_rows)
    mod.candidates(None, to_voice="ttsv_target")
    assert seen, "应当发起一次查询"
    sql = seen[0]
    assert "ttsv_target" in sql
    assert "tts_voice_id is null or d.tts_voice_id <>" in sql


def test_redistill_script_全部重跑(monkeypatch):
    """`--all` ⇒ 不看 tts_voice_id。"""
    mod = _script_module()
    seen: list[str] = []
    monkeypatch.setattr(mod, "_rows", lambda sql: seen.append(sql) or [])
    mod.candidates(None, include_all=True)
    assert "tts_voice_id" not in seen[0]


def test_redistill_script_默认仍是未指定音色的那些(monkeypatch):
    mod = _script_module()
    seen: list[str] = []
    monkeypatch.setattr(mod, "_rows", lambda sql: seen.append(sql) or [])
    mod.candidates(None)
    assert "d.tts_voice_id is null" in seen[0]
