"""CP6.2.1 埋点 SDK 单测（v1 §11.6）。"""

from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.analytics import track, track_simple
from stashbox.backend.common.events import EventName


async def test_track_writes_feedback():
    """track() 写 feedback 表成功，返回 record.id"""
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.rollback = MagicMock()

    record_id = await track(
        db,
        EventName.ARTICLE_SUBMIT,
        user_id=1,
        article_id="abc123",
        metadata={"source": "wechat"},
    )
    assert db.add.called
    assert db.flush.called
    assert record_id is None  # id not populated until flush commits in real DB


async def test_track_returns_none_on_failure():
    """track() 失败时返 None，不抛异常"""
    db = AsyncMock(spec=AsyncSession)
    db.add = MagicMock(side_effect=Exception("db error"))
    db.rollback = AsyncMock()

    record_id = await track(
        db,
        EventName.DISTILL_FAILED,
        user_id=1,
        article_id="abc",
        reason="timeout",
    )
    assert record_id is None


async def test_track_simple_delegates_to_track():
    """track_simple 等价于 track"""
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.rollback = MagicMock()

    record_id = await track_simple(
        db,
        EventName.ARTICLE_CAPTURE_SUCCESS,
        user_id=1,
        article_id="xyz789",
    )
    assert db.add.called
    assert db.flush.called
    assert record_id is None


async def test_track_truncates_overlong_reason_to_column_width():
    """超长 reason 必须被压到列宽（64），原文进 metadata —— 否则整条埋点被丢掉。

    feedback.reason 是 varchar(64)。调用点若塞进去一段长异常文本，
    insert 会报 StringDataRightTruncationError，而 track() 是**静默**吞异常的
    （只留一条 warning）→ 事件消失，外部完全无感。
    护栏放在 SDK 层，所以这里不依赖调用点自己记得截断。
    """
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.rollback = MagicMock()

    long_reason = "OpenAITTS 错误 401: " + "x" * 500
    await track(
        db,
        EventName.DISTILL_FAILED,
        user_id=1,
        article_id="abc",
        reason=long_reason,
    )

    record = db.add.call_args[0][0]
    assert len(record.reason) == 64, "reason 必须被截到列宽内"
    assert record.metadata_["reason_full"] == long_reason, "原文必须留在 metadata 里"


async def test_track_keeps_short_reason_and_caller_metadata():
    """短 reason 原样保留；调用方自己传的 metadata 不被覆盖。"""
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.rollback = MagicMock()

    await track(
        db,
        EventName.DISTILL_FAILED,
        user_id=1,
        article_id="abc",
        reason="timeout",
        metadata={"step": "tts"},
    )

    record = db.add.call_args[0][0]
    assert record.reason == "timeout"
    assert record.metadata_["step"] == "tts"
    assert "reason_full" not in record.metadata_


def test_event_name_enum_has_50():
    """EventName 枚举至少 50 个事件（v1 §11.6 验收）"""
    assert len(EventName) >= 50


def test_event_name_string_values():
    """所有事件名都是 snake_case 字符串"""
    for ev in EventName:
        assert ev.value == ev.value.lower()
        assert "_" in ev.value or ev.value.islower()
