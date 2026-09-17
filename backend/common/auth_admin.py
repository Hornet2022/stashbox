"""admin/operator 鉴权依赖（CP1.8）。v1 §3.6：admin + operator 角色。"""
from fastapi import HTTPException, Depends
from stashbox.backend.common.auth import require_user

ADMIN_TIERS = {"admin", "operator"}


async def require_admin_or_operator(user: dict = Depends(require_user)) -> dict:
    """要求 user.tier ∈ {admin, operator}，否则 403。

    用法：
    ```python
    @router.get("/api/v1/admin/stats")
    async def admin_stats(
        user: dict = Depends(require_admin_or_operator),
        db: AsyncSession = Depends(get_db),
    ):
        ...
    ```
    """
    tier = user.get("tier", "free")
    if tier not in ADMIN_TIERS:
        raise HTTPException(
            status_code=403,
            detail=f"admin role required, current tier: {tier}",
        )
    return user


async def require_admin(user: dict = Depends(require_user)) -> dict:
    """更严：只允许 admin（不含 operator）。"""
    tier = user.get("tier", "free")
    if tier != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"admin role required, current tier: {tier}",
        )
    return user
