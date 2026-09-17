"""CP1.8 admin/operator 鉴权测试（v1 §11.1-CP1.8）。

4 个测试：admin 通行 / operator 通行 / 普通 403 / require_admin 排除 operator。
"""
import pytest
from fastapi import HTTPException
from stashbox.backend.common.auth_admin import require_admin, require_admin_or_operator


@pytest.mark.asyncio
async def test_admin_tier_passes():
    """admin tier 应直接通行，不抛异常。"""
    user = {"sub": "1", "tier": "admin"}
    result = await require_admin_or_operator(user)
    assert result == user


@pytest.mark.asyncio
async def test_operator_tier_passes():
    """operator tier 应直接通行，不抛异常。"""
    user = {"sub": "2", "tier": "operator"}
    result = await require_admin_or_operator(user)
    assert result == user


@pytest.mark.asyncio
async def test_free_tier_403():
    """free tier 应抛 403 HTTPException。"""
    user = {"sub": "3", "tier": "free"}
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_or_operator(user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_excludes_operator():
    """require_admin 只认 admin，operator 应抛 403。"""
    user = {"sub": "4", "tier": "operator"}
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(user)
    assert exc_info.value.status_code == 403
