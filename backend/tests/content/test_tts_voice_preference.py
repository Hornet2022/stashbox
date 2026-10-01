"""CP-TTS-VOICE：用户音色/语速偏好 —— 三态语义 + 并发首写（2026-09-30 自测回归）。

对应 bug：**BUG#5 并发首写 500**。`user_tts_preferences` 的 PK 就是 user_id，
原先 `set_user_preference` 走「SELECT → 没就 INSERT」的读-改-写，两个并发请求
都会 SELECT 到 None 然后都 INSERT，撞主键，未映射的 IntegrityError 直接 500。
实测 6 并发首写稳定 4~5 个 500。

所以这里的核心断言是**并发下不出现 5xx**，而不是「最终值等于谁」——
upsert 的语义是后写覆盖前写，最终值取决于调度顺序，本来就不该钉死。

前置：本机 PG + Redis 已起，且已 `alembic upgrade head`（含 0033）。
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select

from helpers import content_main
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import TTSVoice, User, UserTTSPreference
from stashbox.backend.common.tts_voice_service import (
    UNSET,
    VoiceError,
    set_user_preference,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
async def user_id():
    """建一个一次性用户，测试结束后删掉（FK CASCADE 会带走偏好行）。

    「清理放在断言之后」是本项目 e2e 的既有教训：先删再断言的话，断言失败时
    数据已经没了，复现要重来一遍。这里用 yield 保证清理一定在用例逻辑跑完之后。
    """
    uid = uuid.uuid4().hex[:12]
    async with AsyncSessionLocal() as db:
        row = User(open_id=f"tts_pref_test_{uid}", nickname="tts_pref_test")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        new_id = row.id
    yield new_id
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserTTSPreference).where(UserTTSPreference.user_id == new_id))
        await db.execute(delete(User).where(User.id == new_id))
        await db.commit()


@pytest.fixture
async def active_voice():
    """一条上架音色。软删字段为 None 的行会被 list_voices 过滤，所以只删自己这条。"""
    vid = f"ttsv_{uuid.uuid4().hex[:22]}"
    async with AsyncSessionLocal() as db:
        db.add(
            TTSVoice(
                id=vid,
                slug=f"test_{uuid.uuid4().hex[:12]}",
                display_name="单测音色",
                ref_audio_url="/tmp/does-not-need-to-exist.wav",
                ref_text="参考文本",
                is_active=True,
            )
        )
        await db.commit()
    yield vid
    async with AsyncSessionLocal() as db:
        await db.execute(delete(TTSVoice).where(TTSVoice.id == vid))
        await db.commit()


def _auth(user_id: int) -> dict:
    return {
        "Authorization": f"Bearer {create_access_token(str(user_id))}",
        "Content-Type": "application/json",
    }


def _client():
    return AsyncClient(transport=ASGITransport(app=content_main.app), base_url="http://test")


# ---------------------------------------------------------------------------
# 三态：UNSET / None / 具体值
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_三态_不传音色字段则不动(user_id, active_voice):
    """只改语速不能把音色顺手清掉 —— 三态里最容易回归的一条。"""
    await set_user_preference(user_id, voice_id=active_voice, speed=1.25)

    await set_user_preference(user_id, speed=1.5)

    async with AsyncSessionLocal() as db:
        pref = (
            await db.execute(select(UserTTSPreference).where(UserTTSPreference.user_id == user_id))
        ).scalar_one()
        assert pref.voice_id == active_voice, "只改 speed 不该动 voice_id"
        assert float(pref.speed) == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_三态_显式null清空音色(user_id, active_voice):
    """显式传 null = 「跟随默认音色」。传 UNSET 和传 null 必须区分得开。"""
    await set_user_preference(user_id, voice_id=active_voice)

    await set_user_preference(user_id, voice_id=None)

    async with AsyncSessionLocal() as db:
        pref = (
            await db.execute(select(UserTTSPreference).where(UserTTSPreference.user_id == user_id))
        ).scalar_one()
        assert pref.voice_id is None


@pytest.mark.asyncio
async def test_三态_未传且未传speed直接报错(user_id):
    """两个都不给 = 无意义的空操作，服务层应直接拒，而不是悄悄 upsert 一行。"""
    with pytest.raises(VoiceError):
        await set_user_preference(user_id, voice_id=UNSET, speed=None)


@pytest.mark.asyncio
async def test_下架音色不能被选中(user_id, active_voice):
    """选了也选不上的音色是最典型的假闭环：UI 列表里没有，但设置里显示着。"""
    async with AsyncSessionLocal() as db:
        voice = (await db.execute(select(TTSVoice).where(TTSVoice.id == active_voice))).scalar_one()
        voice.is_active = False
        await db.commit()

    with pytest.raises(VoiceError, match="已下架"):
        await set_user_preference(user_id, voice_id=active_voice)


# ---------------------------------------------------------------------------
# BUG#5：并发首写
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_并发首写是单条原子upsert(user_id, active_voice):
    """**核心回归断言**：写偏好必须是「一条 INSERT ... ON CONFLICT」。

    为什么不用「并发打 N 次看有没有抛异常」这种时序断言：
      - 旧实现在真 HTTP 层（uvicorn + 真实网络抖动）6 并发稳定 4~5 个 500，
        但在 pytest 的 in-process ASGI / 事件循环里**常常一次都不复现** ——
        写出来的测试看着绿，其实什么都没盖住。
      - 而这次要防的东西是**实现方式**：只要又退回「SELECT → 没有就 INSERT」，
        生产环境的竞态就一定会回来，跟测试跑不跑得出来无关。

    所以直接盯住「INSERT 之前不能有 SELECT user_tts_preferences」这条结构不变式。
    时序型压力测试保留在下面两条，作为用户可见症状的补充证据。
    """
    stmts: list[str] = []

    # 注意挂 sync_engine：AsyncEngine 不支持 cursor 级事件，会直接抛
    # NotImplementedError（asyncpg 驱动下事件由底层同步引擎代理）
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "user_tts_preferences" in statement:
            stmts.append(" ".join(statement.split()))

    try:
        await set_user_preference(user_id, voice_id=active_voice, speed=1.5)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert stmts, "根本没抓到语句，测试本身失效了"
    assert len(stmts) == 1, f"应为 1 条语句，实际 {len(stmts)} 条：{stmts}"
    only = stmts[0].upper()
    assert only.startswith("INSERT"), f"应是一条 INSERT：{only}"
    assert "ON CONFLICT" in only, f"缺 ON CONFLICT，并发首写会撞主键：{only}"


@pytest.mark.asyncio
async def test_并发首写不产生IntegrityError(user_id, active_voice):
    """时序型补充证据：同一个新用户并发 8 次「首次写偏好」不应抛任何异常。

    ⚠️ 同样**故意打服务层而不是 HTTP 层**：ASGITransport 同进程内跑，请求会
    天然串行化，旧实现在这里可能一次都不炸。旧实现实际能压出的量级见
    提交记录：15 轮约 104 个异常（其中大半是 IntegrityError）。
    """
    payloads = [
        (active_voice, 1.5),
        (None, 1.0),
        (active_voice, 2.0),
        (None, 0.75),
        (active_voice, 1.25),
        (None, 1.0),
        (active_voice, 1.5),
        (None, 2.0),
    ]

    async def call(voice_id, speed):
        try:
            await set_user_preference(user_id, voice_id=voice_id, speed=speed)
            return None
        except Exception as exc:  # noqa: BLE001 — 这里要的就是「什么都别漏出来」
            return exc

    results = await asyncio.gather(*[call(v, s) for v, s in payloads])

    bad = [
        (p, type(r).__name__, str(r).splitlines()[0])
        for p, r in zip(payloads, results)
        if r is not None
    ]
    assert not bad, f"并发首写抛出了异常（应为 0）：{bad}"

    # 无论谁赢，库里只能有一行，且字段必须落在合法域内
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(UserTTSPreference).where(UserTTSPreference.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, f"应恰好 1 行，实际 {len(rows)}"
        assert 0.5 <= float(rows[0].speed) <= 3.0


@pytest.mark.asyncio
async def test_并发改音色与语速不互相覆盖(user_id, active_voice):
    """改音色和改语速并发：不能出现「音色改了但语速被回退」这种半状态。

    DO UPDATE 的 set_ 只带本次真正改的列，所以两个请求各写各的列。
    """
    await set_user_preference(user_id, speed=1.25)

    for _ in range(5):
        await asyncio.gather(
            set_user_preference(user_id, speed=1.5),
            set_user_preference(user_id, voice_id=active_voice),
        )

    async with AsyncSessionLocal() as db:
        pref = (
            await db.execute(select(UserTTSPreference).where(UserTTSPreference.user_id == user_id))
        ).scalar_one()
        assert pref.voice_id == active_voice
        assert float(pref.speed) == pytest.approx(1.5), "音色那次写不该把语速打回默认值"


@pytest.mark.asyncio
async def test_并发首写经HTTP不返回500(user_id, active_voice):
    """服务层修好了，HTTP 层也得确认不会把异常漏成 5xx。

    这一条在 in-process ASGI 下**不会**稳定复现旧 bug（见上一条的说明），
    它的作用是守住「服务层异常 → 4xx/2xx，不出现裸 500」这条边界，
    以及并发下仍只落一行。
    """
    async with _client() as c:
        headers = _auth(user_id)
        payload_mix = [
            {"voice_id": active_voice},
            {"speed": 1.5},
            {"speed": 0.75},
            {"voice_id": None},
            {"speed": 2.0},
            {"voice_id": None, "speed": 1.0},
        ]
        rs = await asyncio.gather(
            *[
                c.put("/api/v1/users/me/tts-preference", headers=headers, json=p)
                for p in payload_mix
            ]
        )

    codes = [r.status_code for r in rs]
    assert all(code == 200 for code in codes), (
        f"并发首写出现非 200：{list(zip(payload_mix, codes))}；"
        f"详见 content-service 日志里的 duplicate key / IntegrityError"
    )

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(UserTTSPreference).where(UserTTSPreference.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, f"应恰好 1 行，实际 {len(rows)}"
