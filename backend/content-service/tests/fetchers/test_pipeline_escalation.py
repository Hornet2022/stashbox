"""降级链的行为测试（HTTP → 浏览器 → 代理预留）。

## 为什么要用"假通道"而不是真浏览器

真实覆盖浏览器通道要起 Chromium（~1.0s/次）并且会去导航真实 URL ——
那既慢又不可控。而**这套代码最该被测的不是"Chromium 能不能渲染"**（那是
Playwright 的责任，本机已实测 0.52s 能渲染成功），而是**"该不该升级"的决策**：

- 404 文章已删 → 换渲染引擎、换 IP 都不会让它复活，必须**立刻**上抛（别让用户等下一级）
- 429 被限流 → 同一 IP 继续打只会更糟，必须换通道
- 解析不到正文 → 典型 JS 渲染页症状，浏览器通道一次命中
- 预算耗尽 → 不能再开新通道，把攒到的错误抛出去

这些决策用假通道就能完整覆盖，且是纯逻辑、零外部依赖、毫秒级。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]
if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load(name: str, pkg_dir: Path = CONTENT_SERVICE_DIR / "fetchers"):
    """按别名加载被测包。

    content-service 目录带连字符 + 本测试包自己也叫 `fetchers`，所以
    `import fetchers` 会命中**测试包**而不是被测包 —— 必须用 importlib 别名，
    和 test_wechat.py / test_generic_url.py 同一个套路。
    """
    spec = importlib.util.spec_from_file_location(
        name, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("cs_pipeline")
_base = importlib.import_module("cs_pipeline.base")
_net = importlib.import_module("cs_pipeline.net")
_pipeline = importlib.import_module("cs_pipeline.pipeline")

FetcherError = _base.FetcherError
FetcherErrorCode = _base.FetcherErrorCode
FetchResult = _base.FetchResult
Deadline = _net.Deadline
ESCALATABLE_CODES = _pipeline.ESCALATABLE_CODES
Acquired = _pipeline.Acquired
Tier = _pipeline.Tier
fetch_with_escalation = _pipeline.fetch_with_escalation

SOURCE = "test"


def _ok_acquirer(tier: str, *, html: str = "<html>ok</html>"):
    async def _acquire():
        return Acquired(
            html=html,
            final_url="https://example.com/a",
            status=200,
            tier=tier,
            url="https://example.com/a",
        )

    return _acquire


def _fail_acquirer(code: FetcherErrorCode, *, msg: str = "boom"):
    async def _acquire():
        raise FetcherError(code=code, message=msg, source=SOURCE)

    return _acquire


def _parse_ok(html: str, final_url: str, status):
    return FetchResult(
        url=final_url, title="t", content_html=html, content_text="正文" * 20, source=SOURCE
    )


# -- 升级集合 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        FetcherErrorCode.NETWORK,
        FetcherErrorCode.RATE_LIMIT,
        FetcherErrorCode.AUTH,
        FetcherErrorCode.PARSE,
    ],
)
def test_transient_codes_escalate(code):
    """瞬时类错误必须升级：换通道（换渲染引擎 / 换出口 IP）有机会救回来。"""
    assert code in ESCALATABLE_CODES


@pytest.mark.parametrize(
    "code",
    [FetcherErrorCode.NOT_FOUND, FetcherErrorCode.UNSUPPORTED, FetcherErrorCode.INTERNAL],
)
def test_deterministic_codes_do_not_escalate(code):
    """确定性错误不升级：文章被删 / 不支持 / 这是我们的 bug，换通道都一样。"""
    assert code not in ESCALATABLE_CODES


# -- 链的行为 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tier_success_records_tier_metadata():
    result = await fetch_with_escalation(
        url="https://example.com/a",
        parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
        tiers=[Tier("http", _ok_acquirer("http")), Tier("browser", _ok_acquirer("browser"))],
        deadline=Deadline(5.0),
        source=SOURCE,
    )
    assert result.raw_metadata["fetch_tier"] == "http"
    assert result.raw_metadata["fetch_tiers_tried"] == ["http"]


@pytest.mark.asyncio
async def test_escalates_to_browser_when_http_fails_with_auth():
    """AUTH（被判定为非官方客户端）→ 升到浏览器通道并成功。"""
    result = await fetch_with_escalation(
        url="https://example.com/a",
        parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
        tiers=[
            Tier("http", _fail_acquirer(FetcherErrorCode.AUTH, msg="请在微信中打开")),
            Tier("browser", _ok_acquirer("browser")),
        ],
        deadline=Deadline(30.0),
        source=SOURCE,
    )
    assert result.raw_metadata["fetch_tier"] == "browser"
    assert result.raw_metadata["fetch_tiers_tried"] == ["http", "browser"]


@pytest.mark.asyncio
async def test_escalates_when_parse_finds_no_content():
    """最关键的一条：HTTP 拿到 200 但**抽不到正文** = JS 渲染页症状 → 升浏览器。

    这正是改造前 100% 失败的场景（纯 httpx 拿到的 HTML 里根本没有正文）。
    """
    calls: list[str] = []

    def parse(acq: Acquired) -> FetchResult:
        calls.append(acq.tier)
        if acq.tier == "http":
            raise FetcherError(
                code=FetcherErrorCode.PARSE, message="no article content extracted", source=SOURCE
            )
        return _parse_ok(acq.html, acq.final_url, acq.status)

    async def _browser_html():
        return Acquired(
            html="<html>rendered</html>",
            final_url="https://example.com/a",
            status=200,
            tier="browser",
            url="https://example.com/a",
        )

    result = await fetch_with_escalation(
        url="https://example.com/a",
        parse=parse,
        tiers=[Tier("http", _ok_acquirer("http")), Tier("browser", _browser_html)],
        deadline=Deadline(30.0),
        source=SOURCE,
    )
    assert calls == ["http", "browser"]
    assert result.raw_metadata["fetch_tier"] == "browser"


@pytest.mark.asyncio
async def test_not_found_does_not_escalate():
    """404 必须立刻上抛：再开一级通道只会让用户从等 3 秒变成等 30 秒。"""
    tried: list[str] = []

    async def _browser_should_not_run():
        tried.append("browser")
        raise AssertionError("浏览器通道不该被启动")

    with pytest.raises(FetcherError) as ei:
        await fetch_with_escalation(
            url="https://example.com/a",
            parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
            tiers=[
                Tier("http", _fail_acquirer(FetcherErrorCode.NOT_FOUND, msg="http 404")),
                Tier("browser", _browser_should_not_run),
            ],
            deadline=Deadline(30.0),
            source=SOURCE,
        )
    assert ei.value.code is FetcherErrorCode.NOT_FOUND
    assert tried == []


@pytest.mark.asyncio
async def test_browser_unavailable_does_not_overwrite_real_verdict():
    """浏览器挂掉时，**真实结论必须保留** —— 这条是实测抓出来的真 bug。

    修复前：HTTP 通道撞上 SSRF 拦截（NETWORK，可升级）→ 升级到浏览器 →
    浏览器没装 → 降级路径把 last_error 覆盖成"浏览器不可用"。
    于是用户和排障看到的都是"tier browser unavailable"，
    真正的原因（目标指向内网被安全策略拦下）被彻底盖掉。
    """

    async def _browser_unavailable():
        raise RuntimeError("BrowserUnavailable: chromium launch failed")

    with pytest.raises(FetcherError) as ei:
        await fetch_with_escalation(
            url="https://example.com/a",
            parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
            tiers=[
                Tier("http", _fail_acquirer(FetcherErrorCode.PARSE, msg="no article content")),
                Tier("browser", _browser_unavailable),
            ],
            deadline=Deadline(30.0),
            source=SOURCE,
        )
    assert ei.value.code is FetcherErrorCode.PARSE
    assert "no article content" in ei.value.message


@pytest.mark.asyncio
async def test_browser_unavailable_is_the_verdict_when_it_is_the_only_tier():
    """如果浏览器是唯一通道且确实不可用，才由它决定错误 —— 但仍收敛成 FetcherError。"""

    async def _browser_unavailable():
        raise RuntimeError("chromium launch failed")

    with pytest.raises(FetcherError) as ei:
        await fetch_with_escalation(
            url="https://example.com/a",
            parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
            tiers=[Tier("browser", _browser_unavailable)],
            deadline=Deadline(30.0),
            source=SOURCE,
        )
    assert ei.value.code is FetcherErrorCode.NETWORK
    assert "chromium launch failed" in ei.value.message  # 技术细节留给日志


@pytest.mark.asyncio
async def test_browser_unavailable_then_http_ok_still_succeeds():
    """HTTP 成功在前，浏览器压根不该被调用。"""
    result = await fetch_with_escalation(
        url="https://example.com/a",
        parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
        tiers=[
            Tier("http", _ok_acquirer("http")),
            Tier("browser", _fail_acquirer(FetcherErrorCode.NETWORK, msg="不该走到这")),
        ],
        deadline=Deadline(30.0),
        source=SOURCE,
    )
    assert result.raw_metadata["fetch_tier"] == "http"


@pytest.mark.asyncio
async def test_deadline_exhausted_stops_before_opening_next_tier():
    """总时限耗尽就不再开新通道（剪藏是同步接口，用户在等）。"""
    tried: list[str] = []

    async def _slow_fail():
        tried.append("http")
        raise FetcherError(code=FetcherErrorCode.PARSE, message="no content", source=SOURCE)

    async def _browser_should_not_run():
        tried.append("browser")
        raise AssertionError("预算耗尽后不该启动浏览器")

    # 预算 0：进循环第一轮就判定 expired
    with pytest.raises(FetcherError) as ei:
        await fetch_with_escalation(
            url="https://example.com/a",
            parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
            tiers=[Tier("http", _slow_fail), Tier("browser", _browser_should_not_run)],
            deadline=Deadline(0.0),
            source=SOURCE,
        )
    assert tried == []
    assert ei.value.code is FetcherErrorCode.NETWORK
    assert "budget exhausted" in ei.value.message


@pytest.mark.asyncio
async def test_all_tiers_fail_raises_last_error():
    """全挂时抛**最后一节**的错误：最后一节通常是成本最高、结论最强的那次尝试。"""
    with pytest.raises(FetcherError) as ei:
        await fetch_with_escalation(
            url="https://example.com/a",
            parse=lambda acq: _parse_ok(acq.html, acq.final_url, acq.status),
            tiers=[
                Tier("http", _fail_acquirer(FetcherErrorCode.NETWORK, msg="http 通道挂了")),
                Tier("browser", _fail_acquirer(FetcherErrorCode.AUTH, msg="浏览器通道也被挡了")),
            ],
            deadline=Deadline(30.0),
            source=SOURCE,
        )
    assert ei.value.code is FetcherErrorCode.AUTH
    assert "浏览器通道也被挡了" in ei.value.message


# -- Deadline 本身 ----------------------------------------------------------


def test_deadline_slice_takes_min_of_cap_and_remaining():
    d = Deadline(5.0)
    assert d.slice(20.0) <= 5.0  # 剩余比本级上限小 → 取剩余
    assert not d.expired()


def test_deadline_unlimited_returns_cap():
    d = Deadline(None)
    assert d.unlimited is True
    assert d.slice(12.0) == 12.0
    assert d.expired() is False


def test_deadline_expires():
    d = Deadline(0.0)
    assert d.expired() is True
    assert d.slice(5.0) == 0.0
