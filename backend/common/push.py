"""推送投递（provider 抽象 + 诚实的状态回报）（2026-10-21）

## 之前是什么样

`push_notifications` 表只记"要推什么"，**没有任何地方记"往哪台设备推"**：

- 没有 `device_token` 列（2026-10-21 补了 `device_tokens` 表）
- 没有投递 worker
- 管理后台的「失败重发」是**假重发**：`user-service/main.py` 的
  `admin_retry_push_notification` 直接把 `status` 改成 `'sent'`、
  `error=NULL`、`sent_at=now`，**不发起任何投递请求**。

后果：运营在后台看到"已发送"，用户手机上什么都没有，而系统里一切正常。
这是最坏的一类缺陷 —— 它让"推送没做"这件事看起来"做了"。

## 现在的口径

`status` 严格表示**投递状态**，不再兼任"这条通知存在"：

- ``pending``：已创建，尚未尝试投递
- ``sending``：投递中（已被某个 worker 认领）
- ``sent``：投递成功（至少有一台设备确认收到）
- ``failed``：投递失败，且**写明原因**

注意：这条通知**同时**还会在应用内的「通知中心」出现（客户端拉
``GET /api/v1/notifications``，那个接口按 ``read_at`` 过滤，不看 status）。
所以"投递失败"不会让用户看不到通知 —— 这是"推送"与"站内通知"两件事，
以前用一个 status 字段混着表达，现在拆清楚了。

## Provider 为什么是抽象的

真推送要连厂商通道（极光 / 腾讯 TPNS / 华为/小米/OPPO/vivo 各自通道），
每家的 API 形状、鉴权方式、错误码都不一样，而且都需要**厂商账号**。

所以这里只定义接口 + 两个实现：

- :class:`NullPushProvider` —— 没配置通道时的默认实现。
  **它如实报告"没发出去"**，而不是假装成功。这是最重要的一条：
  没有通道 ≠ 推送成功。
- :class:`WebhookPushProvider` —— 把推送交给一个统一的 HTTP 网关。
  极光/TPNS/自建网关都能接在这一层后面，换厂商只改配置不改代码。

真要直连某家厂商，新增一个 :class:`PushProvider` 子类即可，
投递流程（认领 → 发送 → 写状态）不用动。
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushTarget:
    """一台待投递的设备。"""

    device_id: int
    token: str
    provider: str


@dataclass
class PushResult:
    """一次投递的结果。

    ``ok`` 的含义是"**确实发出去了**"，不是"调用没报错"。
    """

    ok: bool
    provider: str
    detail: str = ""
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    #: 每台设备的失败原因（provider 名字 → 原因）
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.attempted == 0:
            return self.detail or "没有可投递的设备"
        return f"{self.delivered}/{self.attempted} 送达（{self.provider}）：{self.detail}"


class PushProvider(ABC):
    """推送通道。"""

    #: provider 名字，与 `device_tokens.provider` 对应
    name: str = "base"

    @abstractmethod
    async def send(
        self,
        targets: list[PushTarget],
        *,
        title: str,
        body: str,
        deeplink: str | None = None,
    ) -> PushResult:
        """把一条消息投给这些设备。"""


class NullPushProvider(PushProvider):
    """没有配置任何通道时的默认实现。

    **它返回 ok=False，并把原因写清楚。**

    这一条是刻意设计的：之前管理后台的"重发"就是靠"把状态改成 sent"
    来假装成功的。现在没有通道就如实说没有通道 —— 运营看到的是
    "未配置推送通道"，而不是一个绿色的"已发送"。
    """

    name = "none"

    async def send(
        self,
        targets: list[PushTarget],
        *,
        title: str,
        body: str,
        deeplink: str | None = None,
    ) -> PushResult:
        return PushResult(
            ok=False,
            provider=self.name,
            detail="未配置推送通道（设置 STASHBOX_PUSH_WEBHOOK_URL 后接入）",
            attempted=len(targets),
            delivered=0,
            failed=len(targets),
        )


class WebhookPushProvider(PushProvider):
    """把推送交给一个统一的 HTTP 网关。

    请求形状（POST JSON）::

        {
          "title": "...",
          "body": "...",
          "deeplink": "stashbox://detail/xxx" | null,
          "targets": [{"token": "...", "provider": "..."}]
        }

    鉴权：``Authorization: Bearer <STASHBOX_PUSH_WEBHOOK_TOKEN>``（可选）。

    为什么要这一层而不是直接写死某家厂商：
    - 厂商 API 各有形状，换厂商不该改业务代码
    - 厂商账号 / SDK key 是**外部依赖**，不配就不能假装能推
    - 网关形态还能顺手做多通道扇出、限流、失败重试
    """

    name = "webhook"

    def __init__(self, endpoint: str, token: str = "", timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout

    async def send(
        self,
        targets: list[PushTarget],
        *,
        title: str,
        body: str,
        deeplink: str | None = None,
    ) -> PushResult:
        if not targets:
            return PushResult(ok=False, provider=self.name, detail="没有可投递的设备", attempted=0)

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        payload = {
            "title": title,
            "body": body,
            "deeplink": deeplink,
            "targets": [{"token": t.token, "provider": t.provider} for t in targets],
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.endpoint, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return PushResult(
                ok=False,
                provider=self.name,
                detail=f"推送网关请求失败：{type(exc).__name__}",
                attempted=len(targets),
                delivered=0,
                failed=len(targets),
            )

        if resp.status_code >= 400:
            return PushResult(
                ok=False,
                provider=self.name,
                detail=f"推送网关返回 {resp.status_code}",
                attempted=len(targets),
                delivered=0,
                failed=len(targets),
            )

        # 网关返回 {"delivered": N} 视为送达台数；没有就按"请求成功即全送达"算。
        delivered = len(targets)
        try:
            data = resp.json()
            if isinstance(data, dict) and "delivered" in data:
                delivered = int(data["delivered"])
        except Exception:  # noqa: BLE001
            pass

        failed = max(0, len(targets) - delivered)
        return PushResult(
            ok=delivered > 0,
            provider=self.name,
            detail=f"网关 {resp.status_code}",
            attempted=len(targets),
            delivered=delivered,
            failed=failed,
        )


def get_push_provider() -> PushProvider:
    """按环境变量挑一个 provider。

    - ``STASHBOX_PUSH_WEBHOOK_URL`` 有值 → :class:`WebhookPushProvider`
    - 否则 → :class:`NullPushProvider`（如实报告"没配置"）
    """
    endpoint = (os.getenv("STASHBOX_PUSH_WEBHOOK_URL", "") or "").strip()
    if endpoint:
        return WebhookPushProvider(
            endpoint=endpoint,
            token=(os.getenv("STASHBOX_PUSH_WEBHOOK_TOKEN", "") or "").strip(),
            timeout=float(os.getenv("STASHBOX_PUSH_WEBHOOK_TIMEOUT", "10") or 10),
        )
    return NullPushProvider()
