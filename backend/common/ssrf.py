"""
SSRF 防护 —— 拒绝指向内网/回环/链路本地地址的抓取目标。

## 为什么需要

所有「用户给个 URL 我们去抓」的入口都走 `content-service/fetchers/generic_url.py`，
而该 fetcher 是 catch-all（`supports()` 恒返回 True），入参校验只有
「scheme ∈ {http, https}」+「netloc 非空」。

后果（实测可复现）：把 `http://127.0.0.1:6379/`、`http://127.0.0.1:5432/`、
`http://169.254.169.254/latest/meta-data/` 交给任意一个建文章入口
（D9 / clawbot / 微信回调 / `POST /api/v1/articles`），请求会由 content-service
进程自身发出。这绕过了「敏感服务只监听 127.0.0.1」这层防线（PG 5432、Redis 6379、
embedding 8008、TTS 8010、rerank 8011 实测均为回环绑定），因为发起方是本机进程。

Redis **无密码**，是这条链上后果最重的一跳。

## 拦点选型（实测决定，不是猜的）

用 httpx 的 **request event hook**，因为实测确认它在**每次请求真正发出之前**触发，
且 `follow_redirects=True` 时**每一跳重定向都会重新触发**：

    ('request', 'http://example.com/start')
    ('transport', 'http://example.com/start')          <- 真正发出
    ('response', 302)
    ('request', 'http://127.0.0.1/blocked')           <- 拦在这里，还没发出
    ('transport', 'http://127.0.0.1/blocked')         <- 永远不会到这里

用 response hook 就晚了：302 的 response hook 触发时那一跳**已经发出去了**，
白名单能被 302 绕过。

## 残余风险（说清楚，不假装解决）

DNS rebinding：hook 里解析一次 DNS 做校验，httpx 随后发请求时会**再解析一次**，
两次之间存在理论窗口。彻底消除需要把已校验的 IP 直接钉进连接
（自定义 transport / `AsyncHTTPTransport` 里换掉解析结果），代价是要接管 httpx 的连接层。
当前实现收窄了窗口但没有根除；本项目所有抓取目标都是**用户提交的具体文章链接**
而非攻击者自持的域名服务，rebinding 的可利用性低。真正的根治点是把这一层记在
安全待办里，而不是假装已经安全。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from httpx import Request

#: 这些网段不是"公网文章站点"，抓它们只会打到本机/内网基础设施。
_BLOCKED_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),  # 回环
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地 —— 含云元数据 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # 组播
    ipaddress.ip_network("240.0.0.0/4"),  # 保留
)

_BLOCKED_V6 = (
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("::1/128"),  # 回环
    ipaddress.ip_network("fc00::/7"),  # unique-local
    ipaddress.ip_network("fe80::/10"),  # 链路本地
    ipaddress.ip_network("ff00::/8"),  # 组播
    ipaddress.ip_network("2001:db8::/32"),  # 文档
)


class SsrfBlocked(Exception):
    """目标指向内网/回环/链路本地，判定为 SSRF 尝试。"""


def is_blocked_ip(ip: str | ipaddress._BaseAddress) -> bool:
    """该 IP 是否属于禁止抓取的网段。"""
    try:
        addr = (
            ip
            if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(ip)
        )
    except ValueError:
        return True  # 解析不出来一律拒绝（fail-closed）
    # IPv4-mapped IPv6（::ffff:127.0.0.1）是最经典的绕过，必须先剥掉
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    if addr.is_private:
        return True
    nets = _BLOCKED_V6 if addr.version == 6 else _BLOCKED_V4
    return any(addr in net for net in nets)


def check_host(host: str) -> list[str]:
    """解析 host 并检查其全部 A/AAAA 记录，返回解析出的 IP 字符串列表。

    - host 本身是 IP 字面量 → 直接判定
    - 域名 → getaddrinfo 解析，**全部**解析结果都必须是公网
      （只检查第一个会把 A 记录指向 127.0.0.1 的域名放过去）
    """
    if not host:
        raise SsrfBlocked("empty host")
    # IPv6 字面量在 urlparse 的 netloc 里带方括号
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        if is_blocked_ip(bare):
            raise SsrfBlocked(f"target host is a blocked address: {bare}")
        return [bare]

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"cannot resolve host: {host}") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise SsrfBlocked(f"host resolved to nothing: {host}")
    bad = sorted(ip for ip in ips if is_blocked_ip(ip))
    if bad:
        raise SsrfBlocked(f"host {host} resolves to blocked address: {', '.join(bad)}")
    return sorted(ips)


def assert_public_url(url: str) -> list[str]:
    """校验一个抓取目标 URL，合法返回解析出的 IP 列表，否则抛 SsrfBlocked。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SsrfBlocked(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise SsrfBlocked(f"no host in url: {url}")
    return check_host(parsed.hostname)


def guard_request(request: Request) -> None:
    """httpx request event hook：在真正发出之前拦下内网目标。"""
    assert_public_url(str(request.url))
