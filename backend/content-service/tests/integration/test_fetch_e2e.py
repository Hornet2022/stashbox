"""CP2.7 抓取层集成测：10 个真实 URL 端到端（真网络 + 真 DB）。

链路：POST /api/v1/callback/wechat-mp-message（CP2.5 Handler）
      → get_fetcher() 路由到 GenericURLFetcher（CP2.4）
      → 真网络抓 HTML → 抽 title/正文 → 建 articles 行 → 触发蒸馏（替身）

每个 URL 验证：HTTP 200 + article_id 前缀 + article 落库 + title 非空
+ url 匹配 + source='wechat_mp'。

不做的事（v1 §11.2 CP2.7 约束）：
- 不测公众号 / 抖音 URL（WechatFetcher / DouyinFetcher 仍占位抛 2001）
- 不改 fetcher 抽象层 / Handler
- 真实网络失败（超时 / 5xx / 站点抽不到正文）pytest.skip，不判 fail

报告：每个 URL 打一行 `E2E|url|http|title_len|content_len|err|note`，
配合 `-s` 落到 /tmp/cp27_e2e.log，由 generate_report.py 解析成 E2E_REPORT.md
（报告不进 git，见 README.md）。
"""
from __future__ import annotations

import httpx
import pytest

from stashbox.backend.common.models import Article  # noqa: E402

MP_URL = "/api/v1/callback/wechat-mp-message"

# 10 个真实 URL（不重复站点）：
# 2 baseline + 3 社区/博客 + 2 新闻 + 2 技术文档 + 1 中文站点
URLS = [
    "https://example.com/",  # baseline
    "https://www.iana.org/",  # baseline
    "https://www.infoq.cn/",  # 中文技术社区
    "https://lobste.rs/",  # 技术社区首页
    "https://sspai.com/",  # 中文博客
    "https://news.sina.com.cn/",  # 中文新闻
    "https://techcrunch.com/",  # 英文新闻
    "https://www.python.org/",  # 技术文档
    "https://docs.python.org/3/",  # 技术文档
    "https://cn.bing.com/",  # 中文站点
]


def _report(url: str, http_status: int, title_len: int, content_len: int, err: str, note: str):
    """打一行机器可读结果（generate_report.py 解析 `E2E|` 前缀行）。"""
    print(f"E2E|{url}|{http_status}|{title_len}|{content_len}|{err}|{note}", flush=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", URLS)
async def test_fetch_real_url(
    url, content_app, db_setup, redis_setup, ai_client_stub, fetch_recorder
):
    """E2E：发真网络 → 服务号 Handler → 验证 article 入库 + title 非空。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app), base_url="http://test"
    ) as client:
        r = await client.post(
            MP_URL,
            json={
                "from_user": "cp27_openid",
                "text": f"看看 {url}",
                "create_time": 1234567890,
            },
        )

    if r.status_code != 200:
        body = r.json()
        err = str(body.get("code", "-"))
        _report(url, r.status_code, 0, 0, err, body.get("message", "")[:80])
        # 2001（URL 不支持）/ 2002（抓取失败，含超时与抽不到正文）都是站点侧问题
        pytest.skip(f"fetch failed: {r.status_code} code={err} {body.get('message', '')}")

    body = r.json()
    assert body["received"] is True
    article_id = body["article_id"]
    assert article_id.startswith("art_")
    assert body["source"] == "wechat_mp"

    # 1. article 落库（真 PostgreSQL）
    art = await db_setup.get(Article, article_id)
    assert art is not None, f"article {article_id} not in db"
    assert art.title, "title 为空"
    assert art.url == url
    assert art.source == "wechat_mp"

    # 2. 蒸馏触发（替身，不真打 ai-service）
    assert [c["article_id"] for c in ai_client_stub.calls] == [article_id]

    # 3. 正文长度（fetcher 返回值 —— Handler 目前只把 title 落库）
    result = fetch_recorder.get("result")
    content_len = len(result.content_text) if result is not None else -1

    _report(url, 200, len(art.title), content_len, "-", "ok")


# ==========================================================================
# CP2.3.1 抖音真抓集成测（与上面 10 个 URL 同链路，只换抖音 URL）
# ==========================================================================
# 抖音短链（v.douyin.com/iXXXX）走 get_fetcher → DouyinFetcher（移动端 UA 三路径
# fallback）。抖音风控变化快，真抓失败（超时 / 5xx / 三路径全挂）一律 pytest.skip，
# 不判 fail（与上面 URL 同策略）。下面 URL 可能失效，用 parametrize，不硬编码断言。
DOUYIN_LIVE_URLS = [
    "https://v.douyin.com/iJ8N3qWx/",  # 短视频短链
    "https://v.douyin.com/iRhGqKp7/",  # 另一短链（不同类型）
    "https://www.iesdouyin.com/share/video/7123456789012345678/",  # 长 ID 兜底
]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("url", DOUYIN_LIVE_URLS)
async def test_fetch_douyin_real_url(
    url, content_app, db_setup, redis_setup, ai_client_stub, fetch_recorder
):
    """E2E（抖音）：发真网络 → DouyinFetcher 三路径 → 验证 article 入库 + title 非空。

    与 10 URL 同链路（匿名入口 / wechat-mp-message 端点，handler 当前硬编码 source=wechat_mp）。
    真抓失败（超时 / 限流 / 三路径全挂 → 2002）pytest.skip，不误判 fail。
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app), base_url="http://test"
    ) as client:
        r = await client.post(
            MP_URL,
            json={
                "from_user": "cp231_openid",
                "text": f"看看 {url}",
                "create_time": 1234567890,
            },
        )

    if r.status_code != 200:
        body = r.json()
        err = str(body.get("code", "-"))
        _report(url, r.status_code, 0, 0, err, body.get("message", "")[:80])
        # 2001（URL 不支持）/ 2002（抓取失败，含超时与限流）都是站点侧问题
        pytest.skip(f"douyin fetch failed: {r.status_code} code={err} {body.get('message', '')}")

    body = r.json()
    assert body["received"] is True
    article_id = body["article_id"]
    assert article_id.startswith("art_")
    # handler 匿名入口当前硬编码 source=wechat_mp（CP2.5 契约定死，抖音仍走同端点）
    assert body["source"] == "wechat_mp"

    art = await db_setup.get(Article, article_id)
    assert art is not None, f"article {article_id} not in db"
    assert art.title, "title 为空"
    assert art.url == url

    _report(url, 200, len(art.title), -1, "-", "ok")

