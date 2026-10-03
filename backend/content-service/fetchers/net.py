"""抓取网络层（剪藏成功率保障的公共底座）。

## 为什么要这一层

改造前 4 个 fetcher（wechat / douyin / pdf / generic_url）**各自硬编码一份 headers**，
超时还不统一（20 / 30 / 30 / 60 秒），并且全部是**单发请求**：没有重试、没有 cookie
复用、没有代理、没有 JS 兜底。同一套"反爬姿态"散在 4 处，改一处要改 4 处，
漏一处不会报错、只会让线上成功率悄悄低一截。

这一层把跨 fetcher 共用的网络行为收成一处：

- **headers 画像**（`HeaderProfile` / `build_headers`）—— 单一事实来源
- **指数退避重试**（`fetch_with_retry`）—— 瞬时故障一次退避就救回来
- **按 host 节流**（`HostThrottle`）—— 不把自己打成爬虫
- **cookie 会话复用 + 连接池**（`acquire_client`）—— 让目标站看到的是"回访访客"
- **显式代理逃生口**（`proxy_url`）
- **总时限**（`Deadline`）—— 降级链不能变成"用户等 3 分钟"
- **SSRF 防护统一挂载点**（`build_client_kwargs`）—— 见下面的安全说明

## 为什么重试是有收益的（不是"稳妥起见"）

剪藏是**单次用户动作**：用户粘一个链接、按下、期待它变成音频。他不会主动重试第二次
（上一轮改造后抓取失败改成当场报错，虽然比 12 分钟后失败好，但用户的重试率天然很低）。
所以瞬时故障（连接被重置、502 / 503 / 429、CDN 抖动）必须由服务端自己扛掉：
一次 400ms 的退避重试就能把这条链接从"100% 失败"变成"100% 成功"。

反过来，**确定性的失败绝不重试**：404 / 410（文章没了）、UNSUPPORTED（URL 不支持）
重试一万次结果一样，只会白白拖慢用户的等待。

## 安全：SSRF 防护原来只挂了一半（实测可利用）

改造前只有 `generic_url` 挂了 SSRF request hook，wechat / douyin / pdf 三个**全裸**。
而 `get_fetcher` 的匹配顺序是 `[wechat, douyin, pdf, generic_url]` —— 也就是说，
**防护最全的那个反而是最后兜底的**，前面的专属 fetcher 全部绕过防护。

实测可利用（`http://mp.weixin.qq.com@127.0.0.1:8100/health`）：

    get_fetcher 选中: WechatFetcher
    → FetcherError: wechat article body (#js_content) not found (page too small: 39B ...)

39 字节是本机 8100 网关 `/health` 的**真实响应** —— 请求确实由 content-service 进程
发出去了。而同一个 URL 交给 generic_url 会被正确拦下：

    FetcherError: blocked target: target host is a blocked address: 127.0.0.1

`common/ssrf.py` 的注释里写着"所有入口都走 generic_url"——这个前提**当时是错的**，
专属 fetcher 的 `supports()` 用的是子串匹配（`WECHAT_HOST in url`），userinfo 写法
（`mp.weixin.qq.com@127.0.0.1`）照样命中。

修法不是"给三个 fetcher 各补一份 hook"（那正是这次要消灭的分散），而是把挂载点收进
`build_client_kwargs()`：谁建 client 谁自动带防护，新增 fetcher 也不可能漏。

## 残余风险（说清楚，不假装解决）

1. **DNS rebinding**：hook 里解析一次 DNS 做校验，httpx 发请求时会再解析一次，两次之间
   存在理论窗口（`common/ssrf.py` 顶部已记录，本层不重复发明）。
2. **浏览器 Tier 2 另有一套拦点**：Playwright 不走 httpx，hook 不生效，所以
   `browser.py` 必须在自己的导航路由上再拦一次（见该文件）。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

import httpx

if TYPE_CHECKING:  # 只为类型注解；运行时靠 `from __future__ import annotations` 不求值
    from .base import FetcherError

# -- 环境开关 ---------------------------------------------------------------

#: 显式代理（IP 级封锁的逃生口）。默认不配 —— 采购住宅代理是产品决定，不是默认开启。
_ENV_PROXY = "STASHBOX_FETCH_PROXY"
#: 重试基础退避秒数。测试里置 0 避免拖慢用例。
_ENV_RETRY_BASE = "STASHBOX_FETCH_RETRY_BASE_DELAY"
#: Tier 1（HTTP）默认重试次数。1 = 不重试。
_ENV_RETRY_ATTEMPTS = "STASHBOX_FETCH_RETRY_ATTEMPTS"
#: 同一 host 两次请求的最小间隔秒数（节流）。
_ENV_HOST_INTERVAL = "STASHBOX_FETCH_HOST_INTERVAL"

T = TypeVar("T")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# -- headers 画像 ------------------------------------------------------------


class HeaderProfile(str, Enum):
    """浏览器画像。UA / Referer / Accept / Accept-Language 作为一个整体一起变。

    为什么合并成一个画像而不是散着配：`supports()` 匹配的域名决定了该用哪种画像，
    而画像各字段之间有强耦合（iPhone UA 配 desktop Accept-Language 本身就是矛盾信号，
    比单纯换 UA 更可疑）。拆开配迟早会配出不一致的组合。
    """

    WECHAT = "wechat"
    DESKTOP = "desktop"
    MOBILE = "mobile"
    PDF = "pdf"


# 微信 4 件套依据 `docs/2026-09-28_微信公众号文章抓取SOP_v1.0.md`：
#   - UA 带 MicroMessenger 段（微信生态正常形态）
#   - Referer / Accept（含 application/xml）/ Accept-Language（zh-CN）
# SOP §1.4 记的是"任一缺失 = 100% 失败"，所以照齐，不要"精简"。
_WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.45(0x18002d39) "
    "NetType/WIFI Language/zh_CN"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StashBox/0.1"
)
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 Safari/604.1"
)

#: 各画像的 Referer：空串 = 不发这个头（发一个和 UA 不搭的 Referer 比不发更可疑）
_REFERERS: dict[HeaderProfile, str] = {
    HeaderProfile.WECHAT: "https://mp.weixin.qq.com/",
    HeaderProfile.DESKTOP: "",
    HeaderProfile.MOBILE: "",
    HeaderProfile.PDF: "",
}


def build_headers(profile: HeaderProfile) -> dict[str, str]:
    """画像 → httpx headers（UA / Referer / Accept / Accept-Language）。"""
    if profile is HeaderProfile.WECHAT:
        ua, accept = _WECHAT_UA, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    elif profile is HeaderProfile.MOBILE:
        ua, accept = _MOBILE_UA, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
    elif profile is HeaderProfile.PDF:
        ua, accept = _DESKTOP_UA, "application/pdf,*/*;q=0.8"
    else:
        ua, accept = _DESKTOP_UA, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"

    headers = {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        # 明确声明支持压缩：公众号正文普遍 3.5MB，不压缩白耗带宽也更容易触发超时
        "Accept-Encoding": "gzip, deflate, br",
    }
    referer = _REFERERS[profile]
    if referer:
        headers["Referer"] = referer
    return headers


# -- 重试 --------------------------------------------------------------------

#: 值得重试的 HTTP 状态。500 也在列：很多站点用 500 表达"后端瞬时过载"，
#: 对用户来说和 503 没区别。4xx 里的 429 单独算（限流，退避更久）。
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 429 / 503 的退避倍率：被限流说明对方在明确劝退，重试要更客气
_THROTTLED_BACKOFF = 3.0


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS


def retry_attempts() -> int:
    return max(1, _env_int(_ENV_RETRY_ATTEMPTS, 2))


def retry_base_delay() -> float:
    return max(0.0, _env_float(_ENV_RETRY_BASE, 0.6))


def backoff_delay(attempt: int, *, status: int | None = None) -> float:
    """第 attempt 次重试前该等多久（指数退避 + full jitter）。

    用 **full jitter**（`random.uniform(0, exp)`）而不是固定指数：
    并发剪藏时固定退避会让所有请求在同一毫秒齐步重试，正好在目标站看来是一波爬虫；
    full jitter 把重试摊开，业界（AWS "Exponential Backoff and Jitter"）的推荐做法。
    """
    exp = retry_base_delay() * (2 ** max(0, attempt - 1))
    if status in (429, 503):
        exp *= _THROTTLED_BACKOFF
    return random.uniform(0.0, exp)


class Deadline:
    """一次抓取的总时限。

    ## 为什么必须有总时限

    降级链（HTTP → 浏览器 → 代理）每一级都有自己的超时，三级串起来最坏能到分钟级。
    而剪藏是**同步接口**（`_prefetch_for_capture` 就在请求线程里 await 抓取），
    用户盯着一个转圈等。30 秒还没回来，他已经以为应用坏了。

    所以总时限是硬约束：每级只拿"剩余预算"和"本级上限"的较小值，
    预算耗尽就不再开新的一级，直接把最后那个错误抛出去。
    """

    __slots__ = ("_expiry",)

    def __init__(self, budget: float | None) -> None:
        self._expiry: float | None = None if budget is None else time.monotonic() + budget

    @property
    def unlimited(self) -> bool:
        return self._expiry is None

    def remaining(self) -> float | None:
        """剩余秒数；已耗尽返回 0.0，unlimited 返回 None。"""
        if self._expiry is None:
            return None
        return max(0.0, self._expiry - time.monotonic())

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0.0

    def slice(self, cap: float) -> float | None:
        """本级可用预算 = min(本级上限, 剩余)；剩余为 0 时返回 0.0（让调用方别开新请求）。"""
        rem = self.remaining()
        if rem is None:
            return cap
        return min(cap, rem)


async def fetch_with_retry(
    send: Callable[[float], Awaitable[httpx.Response]],
    *,
    deadline: Deadline | None = None,
    attempts: int | None = None,
    on_retry: Callable[[int, str], None] | None = None,
) -> httpx.Response:
    """发一次可重试的请求，返回**最终**响应（含 4xx / 5xx，不在这里抛）。

    参数用 `send(timeout) -> Response` 而不是 `(client, url)`，是为了让调用方
    保留"用哪个 client / 什么 URL"的所有权，也方便测试直接喂一个假 send。

    只对**可重试的失败**重试：网络异常（连接重置 / 超时）和 `RETRYABLE_STATUS`。
    其它情况（404、成功、SSRF 拦截）立刻把结果交回调用方判断。
    """
    total = attempts if attempts is not None else retry_attempts()
    last_exc: Exception | None = None

    for attempt in range(1, total + 1):
        budget = deadline.slice(_PER_ATTEMPT_CAP) if deadline is not None else _PER_ATTEMPT_CAP
        if budget <= 0.0:
            # 总时限已耗尽：不再发新请求，把攒到的错误抛出去
            break
        try:
            response = await send(budget)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= total:
                break
            delay = backoff_delay(attempt)
            if on_retry:
                on_retry(attempt, f"transport error: {type(exc).__name__}")
            await asyncio.sleep(delay)
            continue

        if not is_retryable_status(response.status_code) or attempt >= total:
            return response

        if on_retry:
            on_retry(attempt, f"http {response.status_code}")
        # 关掉再等：连接不还回池子，重试时会重新握手，白丢一次 TCP+TLS
        await response.aclose()
        await asyncio.sleep(backoff_delay(attempt, status=response.status_code))

    if last_exc is not None:
        raise last_exc
    raise httpx.TransportError("retries exhausted without a response", request=None)


#: 单次尝试的请求超时上限。实际会再和 Deadline 剩余预算取小。
_PER_ATTEMPT_CAP = 20.0


# -- 按 host 节流 ------------------------------------------------------------


class HostThrottle:
    """同一 host 的最小请求间隔。

    ## 为什么剪藏服务需要主动限速自己

    抓取成功率不是靠"抓得越猛越高"得来的。用户剪藏是低频动作（一天几次），
    而同一个出口 IP 短时间连打几十个请求，在目标站眼里就是爬虫 —— 于是被限速、
    被拉黑 IP，**之后连正常文章都抓不成了**。为了长期成功率，主动慢一点是划算的。

    间隔默认 0.3 秒：低频剪藏场景下几乎无感（只影响并发抓取同一站的场景），
    但足以让请求在目标站侧呈现"正常人阅读"的节奏。
    """

    def __init__(self, interval: float | None = None) -> None:
        self._interval = interval if interval is not None else _env_float(_ENV_HOST_INTERVAL, 0.3)
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = self._locks[host] = asyncio.Lock()
        return lock

    async def wait(self, host: str) -> float:
        """等到本 host 允许发下一个请求为止，返回实际等待秒数。"""
        if self._interval <= 0.0:
            return 0.0
        async with self._lock_for(host):
            now = time.monotonic()
            gap = now - self._last.get(host, 0.0)
            delay = max(0.0, self._interval - gap)
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._last[host] = now
            return delay


#: 进程级共享节流器：跨 fetcher 生效，否则 wechat 和 generic 各节各的等于没节。
HOST_THROTTLE = HostThrottle()


# -- 代理 --------------------------------------------------------------------


def proxy_url() -> str | None:
    """显式代理（`STASHBOX_FETCH_PROXY`）。没配返回 None。

    注意**不读** `HTTP_PROXY` 环境变量：客户端建 client 时一律 `trust_env=False`
    （CP9.x 定的，因为系统代理会把本机/内网请求拐走）。代理必须是显式配置，
    否则「配了系统代理就意外走代理」这种故障排查起来极难。
    """
    value = (os.environ.get(_ENV_PROXY) or "").strip()
    return value or None


# -- SSRF 统一挂载点 ---------------------------------------------------------


class SsrfBlockedError(httpx.RequestError):
    """SSRF 策略拦下了这次请求。

    必须是**独立类型**而不是复用普通 RequestError：拦下是**终态安全决策**，
    不是"网络没通、换个通道再试试"。混进 NETWORK 会被降级链当成可升级错误，
    于是白白启动一次浏览器（更糟的是浏览器通道的拦截原因会把真正的原因盖掉，
    排障时看到的是"浏览器不可用"而不是"目标指向内网"）。
    """


async def _ssrf_request_hook(request: httpx.Request) -> None:
    """SSRF 拦截：目标解析到内网/回环/链路本地就拒绝发出。

    必须写成 async（httpx 会 `await hook(request)`，传同步函数直接 TypeError）；
    DNS 是阻塞调用，放 `to_thread` 免得卡住事件循环。

    从 `generic_url` 原样搬到这里，成为**所有** fetcher 的统一挂载点
    （原来只有 generic_url 有，另外三个是裸的，见模块顶部安全说明）。
    """
    from stashbox.backend.common.ssrf import SsrfBlocked, assert_public_url

    try:
        await asyncio.to_thread(assert_public_url, str(request.url))
    except SsrfBlocked as exc:
        raise SsrfBlockedError(f"blocked target: {exc}", request=request) from exc


def to_fetcher_error(exc: Exception, *, source: str, timeout: float) -> FetcherError:
    """httpx 异常 → FetcherError（4 个 fetcher 共用一份翻译表）。

    为什么要收在一处：这段 except 块改造前在 4 个 fetcher 里各抄了一遍，
    而"SSRF 拦截要单独成码"这种新规则如果只改 3 个地方，漏的那个就会
    静默地把安全拦截报成"网络故障"——用户看到的是"没能连上这个网站"，
    排障看到的是重试和降级，日志里却没有任何"被安全策略拦下"的痕迹。
    """
    from .base import FetcherError, FetcherErrorCode

    if isinstance(exc, SsrfBlockedError):
        # 对外报 2001（不支持这个链接来源）而不是 2002：
        # 这是**用户提交的 URL 本身有问题**，不是我们服务挂了。
        # 文案刻意不点破"内网/安全策略"，不向攻击者反馈防护是否存在。
        return FetcherError(code=FetcherErrorCode.SSRF_BLOCKED, message=str(exc), source=source)
    if isinstance(exc, httpx.TimeoutException):
        return FetcherError(
            code=FetcherErrorCode.NETWORK, message=f"timeout after {timeout}s", source=source
        )
    return FetcherError(
        code=FetcherErrorCode.NETWORK,
        message=f"request failed: {type(exc).__name__}: {exc}",
        source=source,
    )


def build_client_kwargs(
    profile: HeaderProfile,
    *,
    follow_redirects: bool = True,
    extra_headers: dict[str, str] | None = None,
    guard_ssrf: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """建 httpx client 的统一参数。

    `guard_ssrf=False` 只允许出现在**注入了 MockTransport 的测试**里
    （mock transport 不发真实请求，跑 DNS 校验纯属浪费且会让用例依赖外网）。
    生产路径永远走默认值 True。
    """
    headers = build_headers(profile)
    if extra_headers:
        headers.update(extra_headers)
    kwargs: dict[str, Any] = {
        "follow_redirects": follow_redirects,
        # CP9.x fix：trust_env=False 防止 HTTP_PROXY 把本机/内网请求拐去系统代理。
        # 想走代理必须显式配 STASHBOX_FETCH_PROXY（见 proxy_url 的注释）。
        "trust_env": False,
        "headers": headers,
    }
    if guard_ssrf:
        kwargs["event_hooks"] = {"request": [_ssrf_request_hook]}
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    return kwargs


#: 进程级 client 池。key = (画像, 代理, transport 身份)。
#:
#: 为什么要池而不是每次 `async with httpx.AsyncClient()`：改造前每个 fetcher 的
#: `_download` 都是"建 client → 发一次 → 立刻关"，于是
#:   1. **cookie 全部丢弃**（client 一关，Set-Cookie 随 jar 一起没了）→ 目标站每次
#:      看到的是一个全新访客，比老访客更容易触发风控；
#:   2. 每次重新 TCP + TLS 握手（实测公众号正文 3.5MB，这个开销不小）。
#:
#: transport 是 mock 的话不入池（测试 client 生命周期由用例自己管）。
_CLIENTS: dict[tuple[Any, ...], httpx.AsyncClient] = {}


def acquire_client(profile: HeaderProfile, **kwargs: Any) -> httpx.AsyncClient:
    """取一个可长期复用的 client（无 transport 的生产路径走池）。"""
    transport = kwargs.get("transport")
    if transport is not None:
        return httpx.AsyncClient(**kwargs)
    key = (profile, proxy_url(), kwargs.get("guard_ssrf", True))
    client = _CLIENTS.get(key)
    if client is None:
        client = _CLIENTS[key] = httpx.AsyncClient(**kwargs)
    return client


async def close_clients() -> None:
    """关掉池里所有 client（服务退出 / 测试 teardown 调）。"""
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        await client.aclose()


def reset_clients_for_tests() -> None:
    """测试辅助：丢弃池（不 await close，测试里没有真实连接需要优雅关闭）。"""
    _CLIENTS.clear()


__all__ = [
    "Deadline",
    "HeaderProfile",
    "HostThrottle",
    "HOST_THROTTLE",
    "RETRYABLE_STATUS",
    "acquire_client",
    "backoff_delay",
    "build_client_kwargs",
    "build_headers",
    "close_clients",
    "fetch_with_retry",
    "is_retryable_status",
    "proxy_url",
    "reset_clients_for_tests",
    "retry_attempts",
    "to_fetcher_error",
]
