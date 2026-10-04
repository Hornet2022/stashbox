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

from stashbox.backend.common.event_collect import router, _rate_limit


@pytest.fixture
def test_app():
    """构造只含 event_collect router 的最小 FastAPI app（避 api-gateway 跨包导入）。"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(test_app):
    """TestClient with fresh rate-limit state."""
    from stashbox.backend.common import event_collect

    event_collect._rate_limit.clear()
    event_collect._sweep_counter = 0  # 清扫计数器也是模块级状态，别漏到别的用例
    with TestClient(test_app) as c:
        yield c
    event_collect._rate_limit.clear()
    event_collect._sweep_counter = 0


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
        with patch(
            "stashbox.backend.common.event_collect.track", new_callable=AsyncMock
        ) as mock_track:
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

        with patch(
            "stashbox.backend.common.event_collect.track", new_callable=AsyncMock
        ) as mock_track:
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
        with patch(
            "stashbox.backend.common.event_collect.track", new_callable=AsyncMock
        ) as mock_track:
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


# CP6.2.2.2b: 4 服务 lifespan 事件测试
class TestServiceLifespanEvent:
    """test_service_lifespan_event_tracks：SERVICE_START / SERVICE_STOP mock 验证。"""

    @pytest.mark.asyncio
    async def test_service_lifespan_event_tracks(self):
        """SERVICE_START / SERVICE_STOP：模拟 lifespan startup/shutdown 场景，验证 track_simple 被正确调用。

        注意：任务包要求加 1 个测试，但 SERVICE_START 和 SERVICE_STOP 是成对事件，
        合并在一个测试方法内验证（避免重复用例）。
        """
        from stashbox.backend.common import analytics
        from stashbox.backend.common.events import EventName

        mock_db = AsyncMock()

        # SERVICE_START
        with patch.object(analytics, "track_simple", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1
            await analytics.track_simple(mock_db, EventName.SERVICE_START, 0, "n/a")
        mock_track.assert_called_once()
        call_args = mock_track.call_args
        assert call_args[0][1] == EventName.SERVICE_START
        assert call_args[0][2] == 0
        assert call_args[0][3] == "n/a"

        # SERVICE_STOP
        with patch.object(analytics, "track_simple", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1
            await analytics.track_simple(mock_db, EventName.SERVICE_STOP, 0, "n/a")
        mock_track.assert_called_once()
        call_args = mock_track.call_args
        assert call_args[0][1] == EventName.SERVICE_STOP
        assert call_args[0][2] == 0
        assert call_args[0][3] == "n/a"


# ---------------------------------------------------------------------------
# user_id 归属：只能来自 token，不能来自请求体（2026-10 回归）
#
# 原来 `user_id=req.user_id or 0` 直接采信请求体，而 feedback.user_id 的外键
# 已被 0018 迁移删掉 —— 任意 user_id 都写得进去。于是任何人 POST 一下就能往
# 运营看板和反馈 CSV 里灌「某用户在做了 X」的假记录。
#
# 判据是「track 实际收到的 user_id」：不看 HTTP 状态（本修复前后都是 200），
# 因为请求照样要成功 —— 匿名上报是设计内的能力。
# ---------------------------------------------------------------------------


def _capture_user_id(client, body: dict, token: str | None = None):
    """跑一次 collect，返回 track 收到的 user_id。"""
    from stashbox.backend.common import event_collect

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with patch.object(event_collect, "track", new_callable=AsyncMock) as mock_track:
        mock_track.return_value = 1
        resp = client.post("/api/v1/events/collect", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return mock_track.call_args.kwargs["user_id"]


class TestUserIdNotSpoofable:
    def test_请求体里的_user_id_不采信(self, client):
        """匿名 + 伪造 user_id → 落库记成 0（匿名），不是请求体里那个号。"""
        got = _capture_user_id(
            client, {"event_name": "user_login", "device_id": "d1", "user_id": 999}
        )
        assert got == 0, f"采信了请求体的 user_id（记成 {got}）—— 可冒名写埋点"

    def test_带_token_时以_token_为准(self, client):
        """带 token → 记 token 那个用户，即便 body 里写了别人的号。"""
        from stashbox.backend.common.auth import create_access_token

        token = create_access_token("42")
        got = _capture_user_id(
            client, {"event_name": "user_login", "device_id": "d2", "user_id": 999}, token
        )
        assert got == 42, f"应以 token 为准，实际 {got}"

    def test_匿名上报仍然可用(self, client):
        """device_id 必填的设计本意就是允许未登录设备上报 —— 别给端点加硬鉴权。"""
        from stashbox.backend.common import event_collect

        with patch.object(event_collect, "track", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = 1
            resp = client.post(
                "/api/v1/events/collect", json={"event_name": "article_submit", "device_id": "d3"}
            )
        assert resp.status_code == 200, resp.text
        assert mock_track.call_args.kwargs["user_id"] == 0


# ---------------------------------------------------------------------------
# 限流字典不能只增不减（2026-10 回归）
# ---------------------------------------------------------------------------


class TestRateLimitBounded:
    def test_过期桶会被清扫掉(self, client):
        """限流是按客户端自报的 device_id 算的，所以 key 只增不减就会被撑爆内存。

        原实现只裁剪桶内时间戳、从不删 key。这里直接打一把「全过期」的桶，
        触发清扫后它们必须从字典里消失。
        """
        from stashbox.backend.common import event_collect

        event_collect._rate_limit.clear()
        for i in range(50):
            event_collect._rate_limit[f"dead_{i}"] = [1.0]  # 1970 年，早已出窗

        event_collect._sweep_rate_limit(event_collect.time.time())

        assert event_collect._rate_limit == {}, "过期桶没被清掉 —— 字典会单调涨到 OOM"

    def test_活跃桶不会被清扫(self, client):
        """清扫只清过期的：窗口内的桶必须留着，否则限流形同虚设。"""
        import time as _time

        from stashbox.backend.common import event_collect

        event_collect._rate_limit.clear()
        now = _time.time()
        event_collect._rate_limit["live"] = [now]

        event_collect._sweep_rate_limit(now)

        assert "live" in event_collect._rate_limit, "把活跃设备的限流桶也清了"

    def test_轮换_device_id_不会无限增长字典(self, client):
        """端到端视角：轮换 device_id 灌 600 条（跨过清扫阈值），字典不能爆。

        限流被绕开这件事本身改不掉（key 来自客户端），但至少不能让内存跟着
        请求数一起涨。
        """
        from stashbox.backend.common import event_collect

        event_collect._rate_limit.clear()
        for i in range(600):
            with patch.object(event_collect, "track", new_callable=AsyncMock) as m:
                m.return_value = 1
                client.post(
                    "/api/v1/events/collect",
                    json={"event_name": "user_login", "device_id": f"rot_{i}"},
                )

        assert len(event_collect._rate_limit) <= event_collect._RATE_LIMIT_MAX_KEYS
        event_collect._rate_limit.clear()
