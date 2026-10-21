"""推送投递服务：认领 → 发送 → 写状态（2026-10-21）

这一层只做**编排**：认领哪些通知、发给哪些设备、把结果写回数据库。
真正"怎么发"在 :mod:`stashbox.backend.common.push` 的 provider 里。

状态机（``push_notifications.status``）::

    pending ──认领──> sending ──成功──> sent
                            └─失败──> failed

写状态的原则只有一条：**如实**。
- 没配通道 → failed，原因写"未配置推送通道"
- 用户没注册设备 → failed，原因写"该用户没有已注册的设备"
- 网关报错 → failed，原因写具体错误

以前管理后台的"重发"是把状态直接改成 'sent'（不投递），
所以后台一片绿而用户什么都没收到。现在没有任何路径能凭空产出 'sent'。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models.device_token import DeviceToken
from stashbox.backend.common.models.push_notification import PushNotification
from stashbox.backend.common.push import PushProvider, PushResult, PushTarget, get_push_provider

log = logging.getLogger(__name__)

#: 状态取值
STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def load_targets(db: AsyncSession, user_id: int) -> list[PushTarget]:
    """取这个用户所有**可用**的设备令牌。"""
    res = await db.execute(
        select(DeviceToken).where(
            DeviceToken.user_id == user_id,
            DeviceToken.deleted_at.is_(None),
            DeviceToken.is_active.is_(True),
        )
    )
    return [
        PushTarget(device_id=r.id, token=r.token, provider=r.provider) for r in res.scalars().all()
    ]


async def deliver_notification(
    db: AsyncSession,
    notification: PushNotification,
    *,
    provider: PushProvider | None = None,
) -> PushResult:
    """投递一条通知，并把**真实结果**写回 status / error / sent_at。

    幂等：已经是 sent 的直接返回，不再发一次。
    """
    if notification.status == STATUS_SENT:
        return PushResult(ok=True, provider="skip", detail="已经投递过", attempted=0, delivered=0)

    provider = provider or get_push_provider()
    targets = await load_targets(db, notification.user_id)

    if not targets:
        result = PushResult(
            ok=False,
            provider=provider.name,
            detail="该用户没有已注册的设备（客户端未注册推送令牌）",
            attempted=0,
        )
    else:
        result = await provider.send(
            targets,
            title=notification.title,
            body=notification.body,
            deeplink=notification.deeplink,
        )

    notification.status = STATUS_SENT if result.ok else STATUS_FAILED
    notification.error = None if result.ok else (result.detail or "投递失败")
    notification.sent_at = _now() if result.ok else None

    log.info(
        "push_deliver: notif=%s user=%s provider=%s -> %s",
        notification.id,
        notification.user_id,
        result.provider,
        result.summary(),
    )
    return result


async def claim_pending(db: AsyncSession, *, limit: int = 50) -> list[PushNotification]:
    """认领一批待投递的通知。

    认领用**条件更新**（``UPDATE ... WHERE id=? AND status='pending'``）而不是
    先查后改：多个 ai-service 实例同时跑时，靠 WHERE 里的状态条件保证
    一条通知只被一个实例认领（rowcount=1 才算认领成功）。
    """
    res = await db.execute(
        select(PushNotification)
        .where(PushNotification.status == STATUS_PENDING)
        .order_by(PushNotification.created_at.asc())
        .limit(limit)
    )
    candidates = res.scalars().all()

    claimed: list[PushNotification] = []
    for n in candidates:
        r = await db.execute(
            update(PushNotification)
            .where(PushNotification.id == n.id, PushNotification.status == STATUS_PENDING)
            .values(status=STATUS_SENDING)
        )
        if r.rowcount == 1:
            n.status = STATUS_SENDING
            claimed.append(n)
    if claimed:
        await db.commit()
    return claimed
