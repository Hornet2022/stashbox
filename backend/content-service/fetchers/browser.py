"""Tier 2 抓取通道：无头浏览器（Playwright / Chromium）。

## 它解决什么

纯 httpx 拿不到**必须执行 JavaScript 才有正文**的页面。本机实测（本地最小 SPA，
正文由独立 `/app.js` 在 300ms 后注入）：

    Tier1 httpx    0.02s  可见正文含标记=False  可见文本长度=125   <- 只有一个 loading 壳
    Tier2 browser  0.52s  可见正文含标记=True   可见文本长度=413   <- 拿到渲染后正文

纯 httpx 那条路拿到的 HTML 里**根本没有正文**（正文在 JS 执行后才进 DOM），
`ContentExtractor` 的密度启发式再准也只能对着空壳算密度，必然空。
这类页面今天在听匣是 **100% 抓取失败**，不是"偶尔失败"。

## 为什么单独一层而不是把 Playwright 塞进 wechat / generic_url

反爬姿态的差异是**通道级**的（要不要跑 JS、要不要 cookie、指纹是什么），
不是站点级的。同一个 wechat 抓取器，HTTP 通道用 MicroMessenger UA，
浏览器通道就得用真实 Chromium 上下文（UA 还必须与 JS 侧一致，否则 UA 本身就是破绽）。
把两条通道塞进同一个 fetcher 会让 fetcher 同时负责"怎么拿到 HTML"和"怎么解析"，
正好是这轮要拆掉的那种耦合。

## 三个必须处理的问题

### 1. SSRF：Playwright 不走 httpx，net.py 的钩子对它无效

`net.py` 的 `_ssrf_request_hook` 挂在 httpx 的 request event 上。浏览器自己发请求，
根本不经过 httpx —— 所以钩子一次都不会触发，防护等于不存在。
必须在浏览器侧**再拦一次**：拦所有导航请求（主文档跳转 + 重定向每一跳），
命中内网就 `route.abort()`。子资源（图片/字体/统计）不拦 —— 它们不携带可读数据，
而逐个拦会让每个页面多出几十次 Python 往返。

### 2. 浏览器必须复用，不能每次请求起一个

Playwright 启动 Chromium 实测 ~1.0s，**每个剪藏都付一次**是不可接受的。
但复用要复用到 **Browser** 这一层，**Context 每次新建**：
- Browser（进程）贵，进程内全局唯一；
- Context（浏览器上下文）便宜，隔离 cookie / 缓存，新建是官方推荐做法，
  也让并发抓取互不污染。

### 3. storage_state 持久化：让目标站看到的是"回访访客"

部分站点对**一次性访客**的容忍度远低于回访访客。cookie 存到磁盘，
进程重启后仍然带着"上次访问"的痕迹，而不是每次都从零开始被当新爬虫。
文件路径可配，默认放临时目录（不放仓库，避免把登录态提交进 git）。

## 不可用时必须优雅降级，不能把剪藏打挂

没装 playwright、没下载浏览器二进制、启动失败 —— 这些都应该让 Tier 2
"不存在"，而不是让整个剪藏失败。所以对外只有一个 `BrowserUnavailable`，
由 pipeline 捕获后安静跳到下一级。**剪藏成功率不能因为一个可选依赖而下降。**
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("stashbox.fetch.browser")

_ENV_ENABLED = "STASHBOX_FETCH_BROWSER_TIER"
_ENV_STATE = "STASHBOX_FETCH_BROWSER_STATE"
_ENV_TIMEOUT = "STASHBOX_FETCH_BROWSER_TIMEOUT"
_ENV_HEADLESS = "STASHBOX_FETCH_BROWSER_HEADLESS"

#: 渲染完成后还要等一会再取 HTML：正文常在 DOMContentLoaded 之后才注入。
#: 3.5s 是"足够覆盖绝大多数首屏注入"和"不让用户多等"之间的折中。
_SETTLE_MS = 1200


class BrowserUnavailable(RuntimeError):
    """浏览器通道不可用（没装 / 没下载二进制 / 启动失败）。

    这是**可降级**信号：pipeline 捕获它并静默跳到下一级，
    剪藏不应该因为一个可选依赖而失败。
    """


@dataclass
class RenderedPage:
    """渲染后的页面。

    同时给 `html` 和 `text`：调用方用 `html` 走既有的 fetcher 解析器（不重复实现解析），
    用 `text` 做"这一级到底有没有正文"的快速判断（比跑一遍完整解析便宜得多）。
    """

    html: str
    text: str
    final_url: str
    status: int | None


def browser_tier_enabled() -> bool:
    """浏览器通道开关。默认开；测试关掉（见 content-service/tests/conftest.py）。"""
    return (os.environ.get(_ENV_ENABLED) or "1").strip().lower() not in ("0", "false", "off", "")


def _state_path() -> Path:
    configured = (os.environ.get(_ENV_STATE) or "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "stashbox-browser-state.json"


def browser_timeout_ms() -> int:
    try:
        return int(os.environ.get(_ENV_TIMEOUT) or 20000)
    except ValueError:
        return 20000


# 反检测脚本。**只做低风险高收益的几项**，不做花哨的指纹伪造：
# 目标不是"骗过专业风控"（那既做不到也不该做），而是"别因为最基础的自动化标记
# 被一刀切"。真正被限流时该走的是代理和降频，不是把这里堆成指纹迷宫。
_ANTI_DETECT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
"""


class BrowserRenderer:
    """进程级浏览器通道（Browser 复用，Context 每次新建）。"""

    def __init__(self) -> None:
        self._pw = None  # playwright 实例
        self._browser = None  # 全局唯一 Browser
        self._launch_lock = asyncio.Lock()
        self._state_dirty = False

    async def _ensure_browser(self) -> object:
        """懒启动 + 进程内复用。并发首次调用靠锁串行，避免起出多个 Chromium。"""
        if self._browser is not None:
            return self._browser
        async with self._launch_lock:
            if self._browser is not None:  # 等锁期间别人已经起好了
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BrowserUnavailable(f"playwright not installed: {exc}") from exc

            try:
                self._pw = await async_playwright().start()
                headless = (os.environ.get(_ENV_HEADLESS) or "1").strip().lower() not in (
                    "0",
                    "false",
                    "off",
                )
                self._browser = await self._pw.chromium.launch(
                    headless=headless,
                    args=[
                        # 抹掉 CDP 自动化标记（Playwright 默认会带 webdriver）
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
            except Exception as exc:  # 浏览器二进制没下载 / 启动失败 / 系统缺依赖
                await self._shutdown_playwright()
                raise BrowserUnavailable(f"chromium launch failed: {exc}") from exc
            return self._browser

    async def render(
        self,
        url: str,
        *,
        user_agent: str | None = None,
        timeout: float | None = None,
    ) -> RenderedPage:
        """渲染 url，返回渲染后的 HTML / 可见文本。

        Raises:
            BrowserUnavailable：通道不可用（调用方应降级）
            Exception：渲染本身失败（超时 / DNS / 被 abort），由调用方转 FetcherError
        """
        browser = await self._ensure_browser()
        context = await browser.new_context(**self._context_options(user_agent))
        try:
            await context.route("**/*", self._make_ssrf_guard())
            page = await context.new_page()
            budget_ms = int((timeout or browser_timeout_ms() / 1000) * 1000)
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=max(1000, budget_ms),
            )
            status = getattr(response, "status", None) if response is not None else None
            if status is not None and status >= 400:
                # 浏览器把 4xx/5xx 也照常渲染（拿到的是错误页），这里直接交回状态码，
                # 让 pipeline 用和 HTTP 通道一致的错误码语义去判。
                return RenderedPage(
                    html=await page.content(),
                    text=await page.inner_text("body"),
                    final_url=page.url,
                    status=status,
                )
            await page.wait_for_timeout(_SETTLE_MS)
            html = await page.content()
            try:
                text = await page.inner_text("body")
            except Exception:  # 极端页面（body 被删）不该让整级失败
                text = ""
            return RenderedPage(html=html, text=text, final_url=page.url, status=status)
        finally:
            # Context 无论成败都要关，否则每次抓取泄漏一份 cookie/缓存
            try:
                await self._save_state(context)
            finally:
                await context.close()

    # -- 内部 ---------------------------------------------------------------

    def _context_options(self, user_agent: str | None) -> dict:
        """Context 参数。

        `user_agent` 必须传成**和 HTTP 通道同一个 UA**：浏览器里 JS 读到的
        navigator.userAgent 与 HTTP 头对不上，是最容易被抓到的破绽之一
        （比用不用 headless 更容易被抓）。
        """
        options: dict = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 414, "height": 896},  # 移动端视口：公众号按移动端排版
            "device_scale_factor": 2,
            "is_mobile": True,
            "has_touch": True,
            "ignore_https_errors": True,
        }
        if user_agent:
            options["user_agent"] = user_agent
        state = _state_path()
        if state.is_file():
            try:
                options["storage_state"] = json.loads(state.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                # 状态文件坏了就当没有：不能因为一个 cookie 文件让整条降级链挂掉
                log.warning("browser state unreadable, ignoring: %s", exc)
        return options

    @staticmethod
    def _make_ssrf_guard():
        """构造 SSRF 导航拦截（见模块顶部「SSRF」小节）。

        只拦**导航请求**：主文档跳转和重定向每一跳都算。
        拦不到的地方（子资源）风险可接受 —— 它们不返回可读数据。
        """
        from stashbox.backend.common.ssrf import SsrfBlocked, assert_public_url

        async def _guard(route, request) -> None:
            if not request.is_navigation_request():
                await route.continue_()
                return
            try:
                await asyncio.to_thread(assert_public_url, request.url)
            except SsrfBlocked as exc:
                log.warning("browser_ssrf_blocked: url=%s err=%s", request.url, exc)
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        return _guard

    async def _save_state(self, context) -> None:
        """把 cookie 落盘（回访访客，见模块顶部问题 3）。失败不影响本次抓取。"""
        try:
            await context.storage_state(path=str(_state_path()))
        except Exception as exc:  # noqa: BLE001 —— 存 cookie 失败不该让抓取失败
            log.debug("browser state save failed: %s", exc)

    async def _shutdown_playwright(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
        self._browser = None

    async def aclose(self) -> None:
        """关浏览器（服务退出 / 测试 teardown）。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        await self._shutdown_playwright()


#: 进程级单例
_RENDERER = BrowserRenderer()


def get_renderer() -> BrowserRenderer:
    return _RENDERER


__all__ = [
    "BrowserRenderer",
    "BrowserUnavailable",
    "RenderedPage",
    "browser_tier_enabled",
    "browser_timeout_ms",
    "get_renderer",
]
