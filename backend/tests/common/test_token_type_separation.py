"""refresh token 不能当 access token 用（2026-10 回归）

`common/auth.py` 里两个方向曾经只锁了一半：

  - `decode_refresh_token` 校验 `type == "refresh"`     ← 锁了
  - `require_user` 用的 `decode_token` 什么都不校验       ← 没锁

而两个 token 的签发差异很大：access 不打 type 声明、7 天；refresh 打
`type=refresh`、30 天（`jwt_refresh_expire_minutes`）。于是「只该用于
`/auth/refresh-token`」的 token 能访问全部业务接口，有效期还是 access 的 4 倍多。

为什么这不是客户端兼容问题：安卓端只把 access token 放进 Authorization 头
（`AuthInterceptor` 取的是 `getAccessToken()`），refresh 只出现在 refresh 请求体里。
也就是说没有任何正当调用方依赖这个行为 —— 它是一道从来没关上的门。

三条一起钉住：
  1. refresh 当 Bearer → 401（核心）
  2. access 当 Bearer → 照常（别把正常登录也堵了）
  3. `require_user_optional` 拿 refresh → 视同匿名（返回 None，不给权限），
     与它对「token 无效/过期」的既有行为一致，匿名端点不会被这次改动变成 401
另附：admin 端点本来就 fail-closed（`tier` 缺省 free，refresh 不带 tier → 403），
有专门的负向用例说明「这不是越权提升」。

守卫有效性：把 `require_user` 换回 `decode_token` → 1、3 两条红（拿到 200/用户身份）。
"""

import pytest
from fastapi import HTTPException

from stashbox.backend.common.auth import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    require_user,
    require_user_optional,
)


def _bearer(token: str) -> str:
    """直接调用 FastAPI 依赖时传的是 header **值**本身，不是 dict。"""
    return f"Bearer {token}"


# ---------------------------------------------------------------------------
# 1. 核心：refresh 不能访问业务端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_user_rejects_refresh_token():
    with pytest.raises(HTTPException) as exc:
        await require_user(_bearer(create_refresh_token("42")))
    assert exc.value.status_code == 401
    assert "refresh" in exc.value.detail.lower()


def test_decode_access_token_rejects_refresh():
    with pytest.raises(HTTPException) as exc:
        decode_access_token(create_refresh_token("42"))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 2. access token 必须照常可用（防止把修复做成「全拒」）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_user_accepts_access_token():
    payload = await require_user(_bearer(create_access_token("42")))
    assert payload["sub"] == "42"


def test_decode_access_token_accepts_access_token():
    assert decode_access_token(create_access_token("7"))["sub"] == "7"


@pytest.mark.asyncio
async def test_access_token_keeps_its_extra_claims():
    """带 tier 的 access token 仍能带出 tier —— admin 鉴权依赖它。"""
    token = create_access_token("1", extra={"tier": "admin"})
    payload = decode_access_token(token)
    assert payload.get("tier") == "admin"


# ---------------------------------------------------------------------------
# 3. require_user_optional：拿 refresh 视同匿名，而不是 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_auth_treats_refresh_as_anonymous():
    """匿名端点（如 /events/collect）不能因为这次改动开始 401。"""
    assert await require_user_optional(_bearer(create_refresh_token("42"))) is None


@pytest.mark.asyncio
async def test_optional_auth_still_accepts_access():
    payload = await require_user_optional(_bearer(create_access_token("42")))
    assert payload is not None and payload["sub"] == "42"


@pytest.mark.asyncio
async def test_optional_auth_without_header_is_anonymous():
    assert await require_user_optional(None) is None


# ---------------------------------------------------------------------------
# 4. admin 端点本来就 fail-closed —— 这不是越权提升，是影响面界定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token_cannot_reach_admin_even_before_this_fix():
    """refresh 不带 tier 声明 → `require_admin` 缺省 free → 403。

    刻意写成不依赖本次修复：它刻画的是**原有**的兜底行为。少了这条，
    读者会以为「refresh 能提权」——实际不能，这次修的只是「能访问普通业务接口」。
    """
    from stashbox.backend.common.auth_admin import require_admin, require_admin_or_operator

    refresh_payload = {"sub": "1", "type": "refresh"}  # 真实 refresh token 的声明形状

    for guard in (require_admin, require_admin_or_operator):
        with pytest.raises(HTTPException) as exc:
            await guard(refresh_payload)
        assert exc.value.status_code == 403, f"{guard.__name__} 竟放行了 refresh token"
