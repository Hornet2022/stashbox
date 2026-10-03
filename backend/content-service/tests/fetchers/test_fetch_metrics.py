"""抓取指标（fetch_metrics）的接线测试。

## 为什么指标本身也要测

指标接错是**静默**的：代码照跑、日志照打，只是线上没人看得出抓取在退化成什么。
而"剪藏成功率"恰恰是这个产品唯一不能靠感觉判断的东西 ——
本轮把抓取拆成多层降级通道后，层数越多，**归因**越依赖这些打点是否打在了
正确的位置（比如"失败归到最后一节通道"而不是无脑归到 http）。

所以这里断言的是**打点位置**，不是 prometheus 库的用法。
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
    spec = importlib.util.spec_from_file_location(
        name, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("cs_metrics")
_base = importlib.import_module("cs_metrics.base")
_pipeline = importlib.import_module("cs_metrics.pipeline")
_net = importlib.import_module("cs_metrics.net")

FetcherError = _base.FetcherError
FetcherErrorCode = _base.FetcherErrorCode
FetchResult = _base.FetchResult
Acquired = _pipeline.Acquired
Tier = _pipeline.Tier
fetch_with_escalation = _pipeline.fetch_with_escalation
Deadline = _net.Deadline

from stashbox.backend.common import fetch_metrics  # noqa: E402

SOURCE = "test"


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


@pytest.fixture(autouse=True)
def _reset_counters():
    """每个用例前把计数清零，否则用例之间互相污染（prometheus 计数是进程级的）。"""
    for c in (
        fetch_metrics.fetch_attempts_total,
        fetch_metrics.fetch_escalations_total,
        fetch_metrics.fetch_browser_tier_unavailable_total,
    ):
        c.clear()
    yield


def _ok(tier: str):
    async def _acquire():
        return Acquired(
            html="<html>x</html>",
            final_url="https://e.com/a",
            status=200,
            tier=tier,
            url="https://e.com/a",
        )

    return _acquire


def _fail(code: FetcherErrorCode):
    async def _acquire():
        raise FetcherError(code=code, message="x", source=SOURCE)

    return _acquire


def _parse(acq):
    return FetchResult(
        url=acq.final_url,
        title="t",
        content_html="<p>x</p>",
        content_text="正文" * 40,
        source=SOURCE,
    )


@pytest.mark.asyncio
async def test_success_records_hit_tier():
    """成功必须记在**命中的那一节**通道上，不是笼统的"成功"。

    因为"该往哪加资源"完全取决于这个数：99% 命中 http 就别急着买浏览器算力。
    """
    await fetch_with_escalation(
        url="https://e.com/a",
        parse=_parse,
        tiers=[Tier("http", _ok("http")), Tier("browser", _ok("browser"))],
        deadline=Deadline(10.0),
        source=SOURCE,
    )
    assert _counter_value(fetch_metrics.fetch_attempts_total, tier="http", outcome="success") == 1
    assert (
        _counter_value(fetch_metrics.fetch_attempts_total, tier="browser", outcome="success") == 0
    )


@pytest.mark.asyncio
async def test_escalation_is_counted_with_code_and_from_tier():
    """升级要能回答"从哪一节、因什么原因升级"—— 这决定下次该修哪一层。"""
    await fetch_with_escalation(
        url="https://e.com/a",
        parse=_parse,
        tiers=[
            Tier("http", _fail(FetcherErrorCode.PARSE)),
            Tier("browser", _ok("browser")),
        ],
        deadline=Deadline(10.0),
        source=SOURCE,
    )
    assert (
        _counter_value(
            fetch_metrics.fetch_escalations_total, from_tier="http", code="fetcher.parse"
        )
        == 1
    )
    assert (
        _counter_value(fetch_metrics.fetch_attempts_total, tier="browser", outcome="success") == 1
    )


@pytest.mark.asyncio
async def test_terminal_failure_is_counted_and_not_escalated():
    """终态失败（404）要记失败，且**不能**记升级 —— 它压根没走下一节。"""
    with pytest.raises(FetcherError):
        await fetch_with_escalation(
            url="https://e.com/a",
            parse=_parse,
            tiers=[Tier("http", _fail(FetcherErrorCode.NOT_FOUND))],
            deadline=Deadline(10.0),
            source=SOURCE,
        )
    assert (
        _counter_value(
            fetch_metrics.fetch_attempts_total, tier="http", outcome="fail:fetcher.not_found"
        )
        == 1
    )
    assert (
        _counter_value(
            fetch_metrics.fetch_escalations_total, from_tier="http", code="fetcher.not_found"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_exhausted_chain_records_failure_on_last_tier():
    """全挂时失败要归到**最后一节**通道。

    归到"http"会掩盖真实结论：最后一节才是成本最高、结论最强的那次尝试，
    它的错误码才是线上该修的那个。
    """
    with pytest.raises(FetcherError):
        await fetch_with_escalation(
            url="https://e.com/a",
            parse=_parse,
            tiers=[
                Tier("http", _fail(FetcherErrorCode.NETWORK)),
                Tier("browser", _fail(FetcherErrorCode.AUTH)),
            ],
            deadline=Deadline(10.0),
            source=SOURCE,
        )
    assert (
        _counter_value(
            fetch_metrics.fetch_attempts_total, tier="browser", outcome="fail:fetcher.auth"
        )
        == 1
    )
    assert (
        _counter_value(
            fetch_metrics.fetch_attempts_total, tier="http", outcome="fail:fetcher.network"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_duration_histogram_observes():
    """耗时必须按命中通道记 —— 浏览器通道慢是已知代价，得能单独看。"""
    await fetch_with_escalation(
        url="https://e.com/a",
        parse=_parse,
        tiers=[Tier("browser", _ok("browser"))],
        deadline=Deadline(10.0),
        source=SOURCE,
    )
    assert fetch_metrics.fetch_duration_seconds.labels(tier="browser")._sum.get() > 0


def test_metric_labels_never_contain_url_or_host():
    """指标标签**绝不能**带 URL / 域名。

    两个理由，缺一不可：
      1. 基数无界（用户提交任意链接）会把 Prometheus 内存打爆；
      2. 那等于把用户浏览历史写进指标系统。
    """
    for counter in (
        fetch_metrics.fetch_attempts_total,
        fetch_metrics.fetch_escalations_total,
        fetch_metrics.fetch_duration_seconds,
    ):
        for label in counter._labelnames:
            assert label in ("tier", "outcome", "from_tier", "code"), f"{counter._name}: {label}"
