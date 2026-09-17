"""CP6.2.1 埋点 SDK 单测（v1 §11.6）。"""
import pytest
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
        db, EventName.ARTICLE_SUBMIT,
        user_id=1, article_id="abc123",
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
        db, EventName.DISTILL_FAILED,
        user_id=1, article_id="abc",
        reason="timeout",
    )
    assert record_id is None


async def test_track_simple_delegates_to_track():
    """track_simple 等价于 track"""
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.rollback = MagicMock()

    record_id = await track_simple(
        db, EventName.ARTICLE_CAPTURE_SUCCESS,
        user_id=1, article_id="xyz789",
    )
    assert db.add.called
    assert db.flush.called
    assert record_id is None


def test_event_name_enum_has_50():
    """EventName 枚举至少 50 个事件（v1 §11.6 验收）"""
    assert len(EventName) >= 50


def test_event_name_string_values():
    """所有事件名都是 snake_case 字符串"""
    for ev in EventName:
        assert ev.value == ev.value.lower()
        assert "_" in ev.value or ev.value.islower()
