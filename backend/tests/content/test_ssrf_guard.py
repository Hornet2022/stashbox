"""SSRF 防护回归：抓取器必须拒绝内网/回环/链路本地目标（含重定向绕过）。

为什么这些是**负向测试**：本会话反复吃过的亏是「不触发的守卫比没有守卫更危险」。
既有 fetcher 用例全部用 MockTransport 打公网样例 URL —— 在**完全没有**任何 SSRF 防护
的情况下它们同样会通过，也就是说历史测试对这条防线是瞎的，加了防护也证明不了它生效。

所以这里显式覆盖"必须被拦"的场景，并且单独验证**重定向绕过**：
只在发起前校验一次的实现，挡不住「先回公网、再 302 到 127.0.0.1」这条路。

跑法：cd backend && .venv/bin/python -m pytest tests/content/test_ssrf_guard.py -q
（顶层 conftest 会把 POSTGRES_DB 切到 stashbox_test）
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "content-service"))

from fetchers.base import FetcherError  # noqa: E402
from fetchers.generic_url import GenericURLFetcher  # noqa: E402

from stashbox.backend.common.ssrf import (  # noqa: E402
    SsrfBlocked,
    assert_public_url,
    is_blocked_ip,
)


# ---------------------------------------------------------------------------
# 1. IP 判定本身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # 回环
        "127.53.1.9",  # 回环整段
        "0.0.0.0",  # unspecified
        "10.0.0.5",  # A 段私网
        "172.16.0.1",  # B 段私网
        "192.168.1.1",  # C 段私网
        "169.254.169.254",  # 云元数据
        "100.64.0.1",  # CGNAT
        "::1",  # IPv6 回环
        "fc00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 链路本地
        "::ffff:127.0.0.1",  # IPv4-mapped 绕过
        "0:0:0:0:0:ffff:7f00:1",  # 同一地址的另一种写法
    ],
)
def test_blocked_ips(ip: str):
    assert is_blocked_ip(ip) is True, f"{ip} 应被判为内网地址"


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "223.5.5.5", "2606:4700::1111"])
def test_public_ips_allowed(ip: str):
    assert is_blocked_ip(ip) is False


# ---------------------------------------------------------------------------
# 2. URL 判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",  # 无密码 Redis
        "http://127.0.0.1:5432/",  # Postgres
        "http://localhost:8008/",  # embedding（DNS 解析到 127.0.0.1）
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8010/",
        "file:///etc/passwd",  # scheme 不对
        "http://2130706433/",  # 十进制 IP = 127.0.0.1
        "http://0x7f.0x0.0x0.0x1/",  # 十六进制 IP
    ],
)
def test_blocked_urls(url: str):
    with pytest.raises(SsrfBlocked):
        assert_public_url(url)


# ---------------------------------------------------------------------------
# 3. fetcher 真的把请求拦下来了（不是只有工具函数在报错）
# ---------------------------------------------------------------------------
async def test_fetcher_blocks_loopback_redis():
    """直连无密码 Redis：最坏的一跳。请求不应发出。"""
    fetcher = GenericURLFetcher()
    with pytest.raises(FetcherError) as ei:
        await fetcher.fetch("http://127.0.0.1:6379/")
    assert "blocked target" in ei.value.message


async def test_fetcher_blocks_cloud_metadata():
    fetcher = GenericURLFetcher()
    with pytest.raises(FetcherError):
        await fetcher.fetch("http://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# 4. 重定向绕过 —— 只做发起前校验的实现挡不住这条
# ---------------------------------------------------------------------------
async def test_fetcher_blocks_redirect_to_loopback():
    """公网 URL 先 302 到 127.0.0.1，第二跳必须被拦。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:6379/"})
        # 如果守卫失效，这里会被真的调用到
        raise AssertionError(f"守卫失效：第二跳真的发出了 {request.url}")

    fetcher = GenericURLFetcher(transport=httpx.MockTransport(handler))
    with pytest.raises(FetcherError):
        await fetcher.fetch("http://example.com/article")


async def test_fetcher_allows_public_redirect_chain():
    """反向验证：正常公网重定向不能被误伤（守卫不能把功能也一起拦了）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "http://example.com/final"})
        return httpx.Response(
            200,
            text="<html><head><title>标题</title></head><body>"
            "<p>" + "正文段落内容。" * 80 + "</p></body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    fetcher = GenericURLFetcher(transport=httpx.MockTransport(handler))
    result = await fetcher.fetch("http://example.com/start")
    assert result.title == "标题"
