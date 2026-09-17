"""
CP4.7.1 gateway routing bugfix: POST /api/v1/articles/{id}/distill 必须路由到 ai-service。

覆盖场景：
1. POST /api/v1/articles/{id}/distill 走 gateway → ai-service（不是 content-service → 404）
2. GET /api/v1/articles/{id}/audio-url 走 gateway → content-service（既有路径）

CP4.7-E2E-BACKEND (b2bd9a4) 自报的 [known issues] #1。
"""

import pytest
import httpx

GATEWAY_URL = "http://localhost:8100"


@pytest.mark.asyncio
async def test_distill_routes_to_ai_service_via_gateway():
    """POST /api/v1/articles/{id}/distill 走 gateway 必须到 ai-service，不是 content-service"""

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        # 1. login 拿 token
        login_resp = await client.post(
            "/api/v1/auth/wechat-login",
            json={"code": "test_code_distill_routing"},
        )
        assert login_resp.status_code == 200, f"login failed: {login_resp.text}"
        token = login_resp.json()["access_token"]

        # 2. 通过 gateway 调 distill（fallback 错误时会走到 content-service → 404）
        article_id = "test-article-cp471"
        distill_resp = await client.post(
            f"/api/v1/articles/{article_id}/distill",
            headers={"Authorization": f"Bearer {token}"},
        )
        # ai-service 有 /distill 端点。content-service 没有 → fallback 错误路由会返回裸 404。
        # ai-service 返回 JSON {"code":40400, "message":"article not found"}（业务逻辑 404，路由正确）。
        # 因此：status_code 404 本身不是问题——关键是要有 JSON body（说明是 ai-service 在响应）。
        # 裸 404 无 JSON → content-service（fallback 错误路由）。
        try:
            resp_json = distill_resp.json()
            is_json_response = True
        except Exception:
            is_json_response = False

        assert is_json_response, (
            f"Got non-JSON response {distill_resp.status_code}: {distill_resp.text}. "
            f"This indicates routing to wrong service (content-service → plain 404, no body)."
        )

        # ai-service 业务逻辑 404（article not found）或 202/200（成功）都是路由正确的表现
        assert distill_resp.status_code in (200, 202, 404), (
            f"Expected 200/202/404 from ai-service, got {distill_resp.status_code}: "
            f"{distill_resp.text}"
        )
        # 验证是 ai-service 的业务错误格式（不是 content-service 的裸 404）
        if distill_resp.status_code == 404:
            assert "code" in resp_json and "message" in resp_json, (
                f"ai-service business-logic 404 expected JSON body with code+message, "
                f"got: {distill_resp.text}"
            )


@pytest.mark.asyncio
async def test_audio_url_routes_to_content_service_via_gateway():
    """GET /api/v1/articles/{id}/audio-url 走 gateway → content-service（既有路径，验证 fallback 仍正常）"""

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        # 走 gateway 调用 audio-url（content-service 端点）
        article_id = "test-article-cp471"
        audio_resp = await client.get(
            f"/api/v1/articles/{article_id}/audio-url",
        )
        # content-service 有 /audio-url 端点（可能 200 带 url 或 404 说没准备好）
        # 期望不是 404 表示路由到了正确的 content-service
        # （fallback 走 articles/ → content-service 本来就对此路径正确）
        assert audio_resp.status_code != 404, (
            f"audio-url routing failed: got 404. Response: {audio_resp.text}"
        )
