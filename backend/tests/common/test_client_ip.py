"""真实客户端 IP 解析（2026-10-21）

## 为什么测这个

登录限流按客户端 IP 计数。而 `X-Forwarded-For` 是**客户端可以自己伪造的** ——
在它被无条件透传之前，任何人只要换一个假 IP 就能绕过限流，
无限建号刷免费配额。

反过来，如果一概不信 XFF，那"所有人算一个 IP"，
一个人刷爆就能把其他人全锁在门外。

所以规则必须是：**只有来自可信代理的 XFF 才采信**，而且要取对
（最左边那个非可信地址 = 原始客户端）。
"""

from stashbox.backend.common.ip import (
    client_ip_from_request,
    parse_trusted_proxies,
    resolve_client_ip,
)

TRUSTED = parse_trusted_proxies("10.0.0.0/8, 1.2.3.4, 2001:db8::/32")
UNTRUSTED = parse_trusted_proxies("")


class TestParseTrustedProxies:
    def test_单个ip_当成_32位网段(self):
        import ipaddress

        nets = parse_trusted_proxies("1.2.3.4")
        assert len(nets) == 1
        assert ipaddress.ip_address("1.2.3.4") in nets[0]

    def test_支持_cidr(self):
        nets = parse_trusted_proxies("10.0.0.0/8,192.168.1.0/24")
        assert len(nets) == 2

    def test_空字符串_返回空(self):
        assert parse_trusted_proxies("") == []
        assert parse_trusted_proxies(None) == []

    def test_配错的项被跳过_而不是抛异常(self):
        """配错一个网段不该让整个服务起不来。"""
        nets = parse_trusted_proxies("10.0.0.0/8, 不是IP, 1.2.3.4")
        assert len(nets) == 2

    def test_忽略空白(self):
        assert len(parse_trusted_proxies(" 10.0.0.0/8 , 1.2.3.4 ")) == 2


class TestResolveClientIp:
    def test_直连_忽略伪造的_xff(self):
        """安全底线：不信任的来源带的 XFF 一律忽略。"""
        ip = resolve_client_ip("203.0.113.9", "1.2.3.4", TRUSTED)
        assert ip == "203.0.113.9", "客户端伪造的 XFF 不能被采信"

    def test_无可信代理配置时_同样忽略_xff(self):
        ip = resolve_client_ip("203.0.113.9", "1.2.3.4", UNTRUSTED)
        assert ip == "203.0.113.9"

    def test_经可信代理_采信_xff(self):
        ip = resolve_client_ip("10.0.0.5", "203.0.113.9", TRUSTED)
        assert ip == "203.0.113.9"

    def test_多跳代理_取最左边的非可信地址(self):
        """XFF 是逐跳追加的：最左边是原始客户端。"""
        # 1.2.3.4 本身在 TRUSTED 里（当代理用），所以这里用 198.51.100.7 当客户端
        ip = resolve_client_ip("10.0.0.5", "198.51.100.7, 203.0.113.9, 10.0.0.5", TRUSTED)
        assert ip == "198.51.100.7"

    def test_全链都是可信代理_取最左边(self):
        ip = resolve_client_ip("10.0.0.5", "10.0.0.6, 10.0.0.5", TRUSTED)
        assert ip == "10.0.0.6"

    def test_没有_peer_也用_xff(self):
        assert resolve_client_ip(None, "203.0.113.9", TRUSTED) == "203.0.113.9"

    def test_什么都没有_返回_unknown(self):
        assert resolve_client_ip(None, None, TRUSTED) == "unknown"
        assert resolve_client_ip(None, "", TRUSTED) == "unknown"

    def test_xff_里有垃圾_跳过它(self):
        ip = resolve_client_ip("10.0.0.5", "not-an-ip, 203.0.113.9", TRUSTED)
        assert ip == "203.0.113.9"

    def test_ipv6_也认(self):
        nets = parse_trusted_proxies("2001:db8::/32")
        ip = resolve_client_ip("2001:db8::1", "203.0.113.9", nets)
        assert ip == "203.0.113.9"


class TestClientIpFromRequest:
    def test_走_request(self):
        """FastAPI Request → 真实 IP。"""

        class _Client:
            host = "203.0.113.9"

        class _Req:
            client = _Client()
            headers = {"x-forwarded-for": "1.2.3.4"}

        # peer 不在可信网段 → 忽略 XFF
        assert client_ip_from_request(_Req(), "") == "203.0.113.9"
        assert client_ip_from_request(_Req(), "10.0.0.0/8") == "203.0.113.9"

        class _Req2:
            client = type("C", (), {"host": "10.0.0.5"})()
            headers = {"x-forwarded-for": "198.51.100.7"}

        # peer 在可信网段 → 采信 XFF
        assert (
            client_ip_from_request(_Req2(), "10.0.0.0/8") == "198.51.100.7"
        ), "peer 在可信网段时，XFF 才采信"
