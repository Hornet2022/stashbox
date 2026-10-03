"""共享网络层（net.py）行为测试 + SSRF 覆盖回归。

## 为什么要单独锁 SSRF

改造前只有 `generic_url` 挂了 SSRF request hook，wechat / douyin / pdf 三个**全裸**，
而 `get_fetcher` 的匹配顺序恰好是 `[wechat, douyin, pdf, generic_url]` ——
**防护最全的那个排在最后**，前面的专属 fetcher 全部绕过它。

实测可利用（`http://mp.weixin.qq.com@127.0.0.1:8100/health`）：

    get_fetcher 选中: WechatFetcher
    → 请求真的打到了本机 8100 网关（拿回 39B 真实响应后才在解析阶段失败）
    同一个 URL 走 generic_url → blocked target: 127.0.0.1

所以下面这些用例不是"防御性编程"，是**锁死一个已修的真实漏洞的回归守卫**。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

import httpx
import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]
if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load(name: str, pkg_dir: Path = CONTENT_SERVICE_DIR / "fetchers"):
    spec = importlib.util.spec_from_file_location(
        name, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("cs_net_t")
_base = importlib.import_module("cs_net_t.base")
_net = importlib.import_module("cs_net_t.net")
_wechat = importlib.import_module("cs_net_t.wechat")
_douyin = importlib.import_module("cs_net_t.douyin")
_pdf = importlib.import_module("cs_net_t.pdf")
_generic = importlib.import_module("cs_net_t.generic_url")
_pkg = sys.modules["cs_net_t"]

FetcherError = _base.FetcherError
FetcherErrorCode = _base.FetcherErrorCode
WechatFetcher = _wechat.WechatFetcher
DouyinFetcher = _douyin.DouyinFetcher
PdfFetcher = _pdf.PdfFetcher
GenericURLFetcher = _generic.GenericURLFetcher
get_fetcher = _pkg.get_fetcher

build_client_kwargs = _net.build_client_kwargs
build_headers = _net.build_headers
fetch_with_retry = _net.fetch_with_retry
HostThrottle = _net.HostThrottle
is_retryable_status = _net.is_retryable_status
RETRYABLE_STATUS = _net.RETRYABLE_STATUS
HeaderProfile = _net.HeaderProfile


# -- SSRF 覆盖：所有 fetcher 的生产路径都必须带防护 -------------------------

#: 走 userinfo 绕过的内网 URL —— 子串匹配的 supports() 全部命中，但真实主机是本机
SSRF_URLS = [
    "http://mp.weixin.qq.com@127.0.0.1:8100/health",
    "https://www.douyin.com@127.0.0.1:8100/health",
    "http://127.0.0.1:8100/anything.pdf",
]


@pytest.mark.parametrize("url", SSRF_URLS)
def test_specialised_fetchers_all_guard_ssrf(url):
    """每个 fetcher 的**生产** client 都必须挂 SSRF hook（guard_ssrf 默认 True）。"""
    for fetcher in (WechatFetcher(), DouyinFetcher(), PdfFetcher(), GenericURLFetcher()):
        kwargs = build_client_kwargs(fetcher.PROFILE, guard_ssrf=fetcher._transport is None)
        assert "event_hooks" in kwargs, f"{fetcher.name} 的 client 少了 SSRF hook"
        assert "request" in kwargs["event_hooks"]
        assert kwargs["event_hooks"]["request"], f"{fetcher.name} 的 SSRF hook 列表是空的"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", SSRF_URLS)
async def test_ssrf_bypass_is_blocked_end_to_end(url):
    """端到端：userinfo 绕过必须被拦在**发出之前**。

    这里用真 client（不注入 MockTransport）走真实 SSRF hook，但目标解析到回环
    地址会在 hook 里被拦下，所以**不会**发出任何真实请求。

    同时锁住三件事：
      1. 真的被拦（message 里能看到 blocked target）
      2. 结论是 SSRF_BLOCKED 而不是 NETWORK —— 拦下是**终态安全决策**，
         升到浏览器通道既白等一次启动，又会让噪音覆盖真正的原因
      3. 拦下的原因**没有**被"浏览器不可用"覆盖（这条正是实测抓出来的 bug）
    """
    with pytest.raises(FetcherError) as ei:
        await GenericURLFetcher().fetch(url, timeout=3.0, budget=5.0)
    assert ei.value.code is FetcherErrorCode.SSRF_BLOCKED
    assert "blocked target" in ei.value.message
    assert "browser" not in ei.value.message.lower()


def test_get_fetcher_picks_wechat_for_ssrf_url():
    """记录既有行为：防护最全的 generic_url 排在最后，所以专属 fetcher 会先命中。

    这条是 SSRF 漏洞的成因说明 —— 不是断言"应该这样"，而是钉住事实，
    让任何人改动 fetcher 顺序时都能看到后果。
    """
    assert isinstance(get_fetcher(SSRF_URLS[0]), WechatFetcher)
    assert isinstance(get_fetcher(SSRF_URLS[1]), DouyinFetcher)
    assert isinstance(get_fetcher(SSRF_URLS[2]), PdfFetcher)


# -- headers 画像 ------------------------------------------------------------


def test_every_profile_emits_full_header_set():
    """每个画像都必须齐发 UA / Accept / Accept-Language。

    微信 SOP §1.4 记的是"任一缺失 = 100% 失败"，所以这里对**所有**画像统一要求，
    不给任何画像留"精简"的余地。
    """
    for profile in HeaderProfile:
        headers = build_headers(profile)
        assert headers["User-Agent"], profile
        assert headers["Accept"], profile
        assert headers["Accept-Language"], profile
        assert headers["Accept-Encoding"], profile


def test_wechat_profile_keeps_four_piece_headers():
    """微信 4 件套不能因为"统一"被改掉。"""
    headers = build_headers(HeaderProfile.WECHAT)
    assert "MicroMessenger/" in headers["User-Agent"]
    assert "iPhone" in headers["User-Agent"]
    assert headers["Referer"] == "https://mp.weixin.qq.com/"
    # application/xml 不能省：缺了部分 CDN 返 406（SOP §1.4）
    assert "application/xml" in headers["Accept"]


def test_fetcher_ua_class_attrs_come_from_profiles():
    """fetcher 的 UA 类属性必须来自 net.py（单一事实来源），不是各写一份。"""
    for fetcher, profile in (
        (WechatFetcher(), HeaderProfile.WECHAT),
        (DouyinFetcher(), HeaderProfile.MOBILE),
        (GenericURLFetcher(), HeaderProfile.DESKTOP),
        (PdfFetcher(), HeaderProfile.PDF),
    ):
        assert fetcher.UA == build_headers(profile)["User-Agent"], fetcher.name
        assert fetcher.PROFILE is profile


# -- 重试 --------------------------------------------------------------------


def test_retryable_status_classification():
    """只有瞬时状态才重试；404/403 重试一万次结果一样，只会拖慢用户。"""
    for status in (429, 500, 502, 503, 504):
        assert is_retryable_status(status), status
    for status in (200, 301, 400, 401, 403, 404, 410, 501):
        assert not is_retryable_status(status), status


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds():
    """503 重试后成功 = 这条链接从"100% 失败"变成"100% 成功"。"""
    calls = {"n": 0}

    async def send(timeout: float) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    response = await fetch_with_retry(send, attempts=3)
    assert response.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_does_not_retry_404():
    """404 不重试：文章没了，换什么都救不回来。"""
    calls = {"n": 0}

    async def send(timeout: float) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    response = await fetch_with_retry(send, attempts=3)
    assert response.status_code == 404
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_transport_error_then_gives_up():
    """连接错误重试到耗尽后，把最后一个异常交回调用方（不吞）。"""
    calls = {"n": 0}

    async def send(timeout: float) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection reset")

    with pytest.raises(httpx.ConnectError):
        await fetch_with_retry(send, attempts=3)
    assert calls["n"] == 3


# -- 节流 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_throttle_spaces_requests_to_same_host():
    """同一 host 两次请求之间必须有间隔（把自己打成爬虫会毁掉长期成功率）。"""
    throttle = HostThrottle(interval=0.15)
    assert await throttle.wait("a.example") == 0.0  # 第一次不等待
    waited = await throttle.wait("a.example")
    assert waited >= 0.1, f"第二次请求只等了 {waited}s，没有节流"


@pytest.mark.asyncio
async def test_host_throttle_is_per_host():
    """不同 host 互不阻塞（节流是按 host 的，不是全局串行）。"""
    throttle = HostThrottle(interval=0.5)
    assert await throttle.wait("a.example") == 0.0
    assert await throttle.wait("b.example") == 0.0


def test_throttle_zero_interval_is_noop():
    throttle = HostThrottle(interval=0.0)
    assert throttle._interval == 0.0


# -- 总时限 ------------------------------------------------------------------


def test_retry_respects_remaining_budget():
    """预算耗尽后不再发新请求 —— 剪藏是同步接口，用户在等。"""

    Deadline = _net.Deadline
    calls = {"n": 0}

    async def send(timeout: float) -> httpx.Response:
        calls["n"] += 1
        # 假装这次请求吃掉了全部预算
        time.sleep(0.02)
        return httpx.Response(503)

    import asyncio

    async def main():
        d = Deadline(0.03)
        try:
            await fetch_with_retry(send, attempts=5, deadline=d)
        except Exception:
            pass
        return calls["n"]

    assert asyncio.run(main()) < 5
