"""content-service 测试的全局开关。

## 为什么要在这里关掉浏览器通道

浏览器通道（`fetchers/browser.py`）在**生产默认开启** —— 它是 Tier 2 兜底，
关掉等于剪藏成功率少一档。但测试**绝不能**起真 Chromium：

- 单次 launch 实测 ~1.0s，几十个用例就是几十秒纯等待；
- 更糟的是它会真的去导航测试 URL（`http://test/...`），变成不可控的外网请求。

所以在 conftest（pytest 一定先于测试模块导入）里把环境变量置 0，
让 `browser_tier_enabled()` 读到的就是关闭状态。

这也意味着：**浏览器通道的降级逻辑需要一个不依赖真浏览器的测试替身**，
见 `tests/fetchers/test_pipeline_escalation.py` —— 它用假 channel 验证
"该不该升级"这条决策，而不是靠真浏览器。

## 为什么还要阻断真实传输层

上面关掉的是浏览器通道，但**普通 HTTP 通道**（httpx 默认 transport）仍然
能出网。这不是理论风险：`tests/integration/test_fetch_e2e.py` 打的就是
techcrunch.com / news.sina.com.cn 等 10 个真实站点，它打 `pytest.mark.network`
在 CI 里被排除。

问题在于「靠人不忘打标记」这种约定迟早会被违反：谁加一个出网用例而忘了标记，
CI 就会去访问真实站点 —— 后果不是「慢」，而是整个 job 被外部站点拖死或随机变红。

所以这里直接封死：把 httpx 的**真实**传输层方法替换成抛异常。用 ASGITransport
（打本进程 app）和 MockTransport 的用例完全不受影响，因为它们根本不经过这两个类。
一旦有人写出真出网的用例，它会在这里立刻炸掉，而不是安静地跑 30 秒。
"""

from __future__ import annotations

import os

import pytest


# 退避重试的 sleep 归零：否则每个模拟 5xx 的用例都要真睡 0.6s+
os.environ["STASHBOX_FETCH_RETRY_BASE_DELAY"] = "0"
os.environ["STASHBOX_FETCH_RETRY_ATTEMPTS"] = "3"
# 按 host 节流归零：测试里连续请求同一 host 不该排队
os.environ["STASHBOX_FETCH_HOST_INTERVAL"] = "0"
# 浏览器通道关闭（理由见模块文档）
os.environ.setdefault("STASHBOX_FETCH_BROWSER_TIER", "0")


def _blocked_real_network(request):  # noqa: ANN001, ANN201
    raise AssertionError(
        f"测试试图发起真实网络请求（{getattr(request, 'url', '?')}）。\n"
        "content-service/tests 下不允许出网：外部站点抖动会让 CI 随机变红，\n"
        "而它对「本仓代码有没有坏」提供不了稳定信号。\n"
        "  · 要测本仓逻辑 → 用 httpx.MockTransport / ASGITransport\n"
        "  · 确实要打真实站点 → 在该文件加 pytestmark = pytest.mark.network，"
        "CI 会用 -m 'not network' 排除"
    )


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch, request):
    """把 httpx 的真实传输层封死（理由见模块文档）。

    只拦真实传输（AsyncHTTPTransport / HTTPTransport）；ASGITransport 与
    MockTransport 是另外的类，不受影响 —— 本目录 140 个用例全靠这两个。

    ⚠️ 必须给 ``network`` 标记的用例让路，否则本地 ``-m network`` 会被自己
    的守卫拦下，那批用例就彻底没法用了 —— 一道为了保护 CI 而设的闸门，
    不该反过来把唯一的验证途径堵死。
    """
    import httpx

    if request.node.get_closest_marker("network"):
        return  # 本用例就是要打真实站点（CI 已用 -m 'not network' 排除）

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_real_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_real_network)
