"""fetchers 抽象层单测（CP2.1）。

导入说明：content-service 目录名带连字符，不是合法包名，只能按路径加载；
而且本测试包自身也叫 `fetchers`（tests/fetchers/），pytest 会先把它塞进
sys.modules —— 直接 `import fetchers` 会命中自己，所以用 importlib 以别名
`cs_fetchers` 加载被测包（做法同 tests/content/helpers.py 的 `_load_app`）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load_fetchers():
    pkg_dir = CONTENT_SERVICE_DIR / "fetchers"
    spec = importlib.util.spec_from_file_location(
        "cs_fetchers",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cs_fetchers"] = module
    spec.loader.exec_module(module)
    return module


cs = _load_fetchers()

Fetcher = cs.Fetcher
FetchResult = cs.FetchResult
FetcherError = cs.FetcherError
FetcherErrorCode = cs.FetcherErrorCode
WechatFetcher = cs.WechatFetcher
DouyinFetcher = cs.DouyinFetcher
GenericURLFetcher = cs.GenericURLFetcher
get_fetcher = cs.get_fetcher

ALL_FETCHER_CLASSES = [WechatFetcher, DouyinFetcher, GenericURLFetcher]


def test_factory_routing_priority():
    """wechat > douyin > generic_url 的优先级。"""
    assert get_fetcher("https://mp.weixin.qq.com/s/abc123").name == "wechat_mp"
    assert get_fetcher("https://v.douyin.com/i12345").name == "douyin"
    assert get_fetcher("https://example.com/article").name == "generic_url"
    # generic_url 是兜底（supports 恒 True），所以这里不是 None
    assert get_fetcher("not-a-url").name == "generic_url"


def test_all_fetchers_inherit_abstract_base():
    """3 个 fetcher 都是 Fetcher 子类 + 实现 3 个 abstractmethod。"""
    assert Fetcher.__abstractmethods__ == frozenset({"name", "supports", "fetch"})
    for cls in ALL_FETCHER_CLASSES:
        fetcher = cls()
        assert isinstance(fetcher, Fetcher)
        assert fetcher.name  # 非空
        # supports 返回 bool
        assert isinstance(fetcher.supports("https://example.com"), bool)


def test_every_fetcher_fetch_accepts_the_abc_contract():
    """所有 fetcher 的 `fetch` 签名必须与 ABC 一致（**这条是被真 bug 逼出来的**）。

    上一轮给 `Fetcher.fetch` 加了 `budget` 参数（降级链总时限），
    wechat / generic_url / pdf 都跟上了，**唯独 douyin 漏改**。
    后果是生产上每个抖音链接都会炸：

        TypeError: DouyinFetcher.fetch() got an unexpected keyword argument 'budget'

    而 TypeError 不是 FetcherError，会被 `_prefetch_for_capture` 的兜底 except
    收敛成 2002「抓取出错了」—— 抖音整条链路静默全废，
    且因为 TypeError 不属于任何 FetcherErrorCode，日志里连原因都很难对上。

    抖音是本轮唯一没被端到端碰过的链路，所以这个漏改没被发现。
    签名一致性属于契约，必须由测试守住，不能靠"改到哪算哪"。
    """
    import inspect

    expected = {
        name: p.default
        for name, p in inspect.signature(Fetcher.fetch).parameters.items()
        if name != "self" and p.default is not inspect.Parameter.empty
    }
    for cls in ALL_FETCHER_CLASSES:
        params = inspect.signature(cls.fetch).parameters
        for name, default in expected.items():
            assert name in params, f"{cls.__name__}.fetch 缺参数 {name}"
            assert params[name].default == default, f"{cls.__name__}.fetch.{name} 默认值不一致"
        # 只允许 ABC 声明的关键字参数，多出来的也要看见（改了契约就该同步）
        assert set(params) - {"self", "url"} == set(
            expected
        ), f"{cls.__name__}.fetch 的关键字参数与 ABC 不一致：{set(params) - {'self', 'url'}}"


async def test_fetch_placeholder_raises():
    """CP2.1 占位：wechat / douyin 的 fetch 抛 FetcherError(UNSUPPORTED)。

    CP2.4 起 GenericURLFetcher 已经真实现（不再占位），它的 fetch 行为
    由 tests/fetchers/test_generic_url.py 覆盖，所以这里只剩 2 个占位 fetcher。
    """
    for cls in [WechatFetcher, DouyinFetcher]:
        with pytest.raises(FetcherError) as exc_info:
            await cls().fetch("https://example.com")
        assert exc_info.value.code == FetcherErrorCode.UNSUPPORTED
        assert exc_info.value.source == cls().name


def test_fetcher_error_format():
    """FetcherError 格式：[source] code: message。"""
    err = FetcherError(FetcherErrorCode.NETWORK, "timeout", source="wechat_mp")
    assert "[wechat_mp]" in str(err)
    assert "fetcher.network" in str(err)
    assert "timeout" in str(err)
    assert err.source == "wechat_mp"
    assert err.message == "timeout"


def test_fetch_result_defaults():
    """FetchResult 只有 4 个必填字段，其余有默认值。"""
    result = FetchResult(
        url="https://example.com/a", title="t", content_html="<p/>", content_text="t"
    )
    assert result.author is None
    assert result.publish_time is None
    assert result.media_urls == []
    assert result.raw_metadata == {}
    assert result.source == "unknown"
