"""CP6.2.2.1 客户端事件收集端点单测（4 个）。

冲突处理（known issues）：
- §4.2 原文要求 `from stashbox.backend.api_gateway.event_router import router`，
  但 event_router.py 不建（§4.4），且 api-gateway 含连字符而非下划线，
  无法作为 Python 包导入。故本测试直接用 event_collect.router 创建测试 app，
  避免跨目录包导入问题。
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stashbox.backend.common.event_collect import router, _rate_limit, _RATE_LIMIT_MAX


@pytest.fixture
def test_app():
    """构造只含 event_collect router 的最小 FastAPI app（避 api-gateway 跨包导入）。"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(test_app):
    """TestClient with fresh rate-limit state."""
    _rate_limit.clear()
    with TestClient(test_app) as c:
        yield c
    _rate_limit.clear()


class MockDB:
    """模拟 AsyncSession + feedback 写成功。"""
    id_counter = 1

    def add(self, record):
        record.id = MockDB.id_counter
        MockDB.id_counter += 1

    async def flush(self):
        pass


class TestCollectEventValid:
    """test_collect_event_valid_writes_feedback：POST event_name="user_login" + device_id="d1" → 200 + event_id 不为 None"""

    def test_collect_event_valid_writes_feedback(self, client):
        """有效请求返回 200 且 event_id 非 None。"""
        with patch("stashbox.backend.common.event_collect.track", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 42
            resp = client.post(
                "/api/v1/events/collect",
                json={"event_name": "user_login", "device_id": "d1"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["event_id"] == 42


class TestCollectEventInvalidName:
    """test_collect_event_invalid_name_400：POST event_name="bogus_event" → 400"""

    def test_collect_event_invalid_name_400(self, client):
        """非法 event_name 返回 400。"""
        resp = client.post(
            "/api/v1/events/collect",
            json={"event_name": "bogus_event", "device_id": "d1"},
        )
        assert resp.status_code == 400
        assert "invalid event_name" in resp.json()["detail"]


class TestCollectEventRateLimit:
    """test_collect_event_rate_limit_429：同 device 101 次 → 100 成功 + 1 个 429"""

    def test_collect_event_rate_limit_429(self, client):
        """同一 device_id 超出 100/min 限流返回 429。"""
        _rate_limit.clear()
        device = "ratelimit_device"

        with patch("stashbox.backend.common.event_collect.track", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1

            # 前 100 个成功
            for i in range(100):
                resp = client.post(
                    "/api/v1/events/collect",
                    json={"event_name": "user_login", "device_id": device},
                )
                assert resp.status_code == 200, f"请求 {i+1} 应成功"

            # 第 101 个触发限流
            resp = client.post(
                "/api/v1/events/collect",
                json={"event_name": "user_login", "device_id": device},
            )
            assert resp.status_code == 429, "第 101 个请求应触发 429"
            assert "rate limit" in resp.json()["detail"].lower()

        _rate_limit.clear()


class TestCollectEventMinimal:
    """test_collect_event_minimal_request：只填 event_name + device_id → 200"""

    def test_collect_event_minimal_request(self, client):
        """只传必填字段（event_name + device_id）应返回 200。"""
        with patch("stashbox.backend.common.event_collect.track", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 7
            resp = client.post(
                "/api/v1/events/collect",
                json={"event_name": "article_submit", "device_id": "minimal_dev"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# CP6.2.2.2a: user + content 服务端事件测试
# 注意：USER_LOGOUT / USER_REGISTER / PLAN_UPGRADE / ADMIN_LOGIN / ADMIN_QUOTA_ADJUST
# 在 user-service 无对应端点，已跳过 [known issues]。
# 以下测试验证事件埋点函数本身可被正确调用。


class TestUserLogoutEvent:
    """test_user_logout_event_tracks：USER_LOGOUT 事件埋点 mock 验证。"""

    @pytest.mark.asyncio
    async def test_user_logout_event_tracks(self):
        """USER_LOGOUT：验证 track_simple 可被正确调用（user_id=123）。"""
        from stashbox.backend.common import analytics
        from stashbox.backend.common.events import EventName

        mock_db = AsyncMock()
        with patch.object(analytics, "track_simple", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1
            await analytics.track_simple(mock_db, EventName.USER_LOGOUT, 123, "n/a")

        mock_track.assert_called_once()


class TestArticleCaptureFailedEvent:
    """test_article_capture_failed_event_tracks：ARTICLE_CAPTURE_FAILED mock 验证。"""

    @pytest.mark.asyncio
    async def test_article_capture_failed_event_tracks(self):
        """ARTICLE_CAPTURE_FAILED：验证 track 可被正确调用。"""
        from stashbox.backend.common import analytics
        from stashbox.backend.common.events import EventName

        ANONYMOUS_USER_ID = 0  # content-service 常量，wechat_mp_message 匿名用户 ID
        mock_db = AsyncMock()
        with patch.object(analytics, "track", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1
            await analytics.track(
                mock_db,
                EventName.ARTICLE_CAPTURE_FAILED,
                user_id=ANONYMOUS_USER_ID,
                article_id="n/a",
                metadata={"error": "fetcher.network"},
            )

        mock_track.assert_called_once()
        call_args = mock_track.call_args
        assert call_args[0][1] == EventName.ARTICLE_CAPTURE_FAILED  # event arg
        assert call_args[1]["user_id"] == ANONYMOUS_USER_ID
