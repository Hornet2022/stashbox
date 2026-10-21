"""真实客户端 IP 解析（2026-10-21）

## 为什么需要这个文件

网关后面的 `user-service` 用客户端 IP 做**登录限流**。要拿到真实 IP，
只能靠 `X-Forwarded-For`。但 XFF 是**客户端可以自己伪造的** ——

    curl -H 'X-Forwarded-For: 1.2.3.4' https://api.stashbox.cn/api/v1/auth/wechat-login

于是限流形同虚设：攻击者每次换一个假 IP 就能无限建号刷免费配额。
而"每个人算一个 IP"也是灾难：一个人就能把所有人锁在门外。

标准做法（也是本文件的实现）：
- **peer 不在可信代理列表** → 忽略客户端带来的 XFF，用直连的 peer IP。
  这是默认：直连来的请求，它的 XFF 一定是伪造的。
- **peer 在可信代理列表**（自己的 nginx / SLB / Ingress）→ 信任 XFF，
  取**最左边**那个不在可信列表里的地址（最左边是原始客户端，后面是逐跳追加的代理）。

`TRUSTED_PROXY_IPS` 支持 IP 和 CIDR，逗号分隔，配置在 `common/config.py`。

## ⚠️ 必须和 uvicorn 的 `FORWARDED_ALLOW_IPS` 配对

uvicorn 自带 `ProxyHeadersMiddleware`，**默认 `forwarded_allow_ips=127.0.0.1`**。
它会在 ASGI 层就把 `scope["client"]` 改写成 XFF 里的地址 ——
**在应用代码看到请求之前就已经改了**。

也就是说有两层独立的信任判断：

| 层 | 配置 | 作用 |
|---|---|---|
| ASGI（uvicorn） | `FORWARDED_ALLOW_IPS` | peer 在列表里 → 用 XFF 改写 `scope["client"]` |
| 应用（本模块） | `TRUSTED_PROXY_IPS` | peer 不在列表 → 忽略 XFF，用真 peer |

两处**必须配成同一个代理列表**，否则会出现两种坏结果：

1. **只配 uvicorn、应用层没配** → 代理在另一台机器（SLB/Ingress）时，
   peer = 代理 IP → 所有人算一个 IP → **一个人刷爆就能把其他人全锁在门外**。
2. **两处不一致** → 网关和下游对"这个 IP 是谁"的理解不同，
   限流按 A 计数、审计按 B 记，永远对不上。

`deploy/preflight.sh` 会检查这两项是否都配了。

## 顺带说明：本地为什么能伪造

从 127.0.0.1 发的请求，uvicorn 的默认可信列表里就有 127.0.0.1 →
它会用 XFF 改写 peer → 应用层看到的就是伪造值。
**远端客户端发同样的请求是挡得住的**（peer 不在 127.0.0.1，uvicorn 不改写）。
所以生产上只要 **8100 不对公网开放**（只让 nginx 访问），
伪造面就只剩"同一台机器上的其他进程"。

## 为什么网关要"覆盖"而不是"透传"

网关转发给下游时，把 XFF **重写成自己解析出来的真实 IP**。
这样下游（user-service 的限流）拿到的一定是网关认定的真实客户端，
不依赖"客户端恰好传了什么"。

代价：下游丢失了原始的 XFF 链。对本项目无影响（限流和审计都只要第一个真实 IP）。
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

from fastapi import Request


def parse_trusted_proxies(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """把 `TRUSTED_PROXY_IPS` 解析成网段列表。

    单个 IP 会被当成 /32（或 /128）—— 写法上写 `1.2.3.4` 和 `1.2.3.4/32` 等价。
    解析不了的项**静默跳过**，不抛异常：配错一个网段不该让整个服务起不来。
    """
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def _in_net(addr: str, nets: Iterable) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for net in nets:
        if ip.version == net.version and ip in net:
            return True
    return False


def resolve_client_ip(
    peer: str | None,
    xff: str | None,
    trusted: Iterable,
) -> str:
    """算出真实客户端 IP。

    - peer 不在可信代理里 → 返回 peer（**忽略** XFF，防伪造）
    - peer 在可信代理里 → 解析 XFF，返回最左边的非可信地址；
      全都可信就返回最左边那个
    - 什么都拿不到 → "unknown"
    """
    nets = list(trusted)

    if peer and not _in_net(peer, nets):
        # 直连（或来自不可信来源）：XFF 不可信，只认 peer。
        return peer

    if xff:
        # 只保留**解析得出来**的地址 —— XFF 里有垃圾项时不能把垃圾返回出去
        # （那会变成一个假的 client_ip，污染限流的分桶）。
        candidates = [c.strip() for c in xff.split(",") if c.strip()]
        parsed: list[str] = []
        for c in candidates:
            try:
                ipaddress.ip_address(c)
            except ValueError:
                continue
            parsed.append(c)
        if parsed:
            # 最左边是原始客户端；从左往右找第一个"不是代理自己"的地址。
            for c in parsed:
                if not _in_net(c, nets):
                    return c
            return parsed[0]

    return peer or "unknown"


def client_ip_from_request(request: Request, trusted_raw: str | None = None) -> str:
    """从 FastAPI 请求里拿真实客户端 IP。"""
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    return resolve_client_ip(peer, xff, parse_trusted_proxies(trusted_raw))
