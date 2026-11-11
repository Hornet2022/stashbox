"""content-service 单测 fixture（CP1.7）。

只放 fixture；非 fixture 的 helper 在 helpers.py（避免与 tests/conftest.py 混在一起）。
"""
import pytest

from helpers import FakeAIClient, content_main


@pytest.fixture(autouse=True)
def fake_ai_client(monkeypatch) -> FakeAIClient:
    """默认不真调 ai-service（单测环境没有 8103 在跑）。"""
    fake = FakeAIClient()
    monkeypatch.setattr(content_main, "get_ai_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def no_accidental_network_fetch(monkeypatch):
    """**默认**把 content-service 的抓取层换成「立刻失败」的假抓取器。

    为什么需要（2026-11-11）：剪藏改成**后台抓取**之后，抓取发生在
    `asyncio.create_task` 出来的独立任务里 ⇒ 用例结束得早于抓取完成时，
    那个任务会**跑到下一个用例、甚至真的访问外网**（实测：一条没 mock
    抓取层的用例让整轮测试挂在收尾阶段，还顺手去访问了 mp.weixin.qq.com）。

    ⇒ 默认给一个「立刻失败」的假抓取器：真的漏了 mock 的用例会**快速失败**
    （文章变 failed），而不是挂住或偷偷出网。
    需要真实成功路径的用例，用自己的 fixture 覆盖它（fixture 顺序：
    conftest 的 autouse 先装，用例自己请求的 fixture 后装 ⇒ 后者生效）。
    """
    from fetchers.base import FetcherError, FetcherErrorCode

    class _NoNetworkFetcher:
        async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
            raise FetcherError(FetcherErrorCode.NETWORK, "network disabled in tests", source="generic_url")

    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: _NoNetworkFetcher())
