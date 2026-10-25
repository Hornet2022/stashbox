"""e2e 专用：假页面抓取器（**不发任何真实请求**）。

## 为什么需要它

2026-10-03 起，剪藏与 D9 入口改成「**先抓后扣**」—— 抓不到正文就当场报错、
不建文章（`content-service/main.py::_prefetch_for_capture` 的说明里记了原因：
抓取失败被吞掉会连锁放大成「扣了配额 + 派了注定失败的蒸馏任务 + 用户 12 分钟后
才看到失败」）。

而 e2e 栈是**本机**跑的，抓取器又带 SSRF 防护（挡回环/私网）⇒
e2e 夹具里那些假 URL（`https://mp.weixin.qq.com/s/e2e_<时间戳>`）**必然 502**。

实测（2026-10-25）：admin-web 的跨端用例报
`502 {"code":2002,"message":"没能提取出文章正文…"}`；后端真机 e2e 的
`test_e2e_04_capture_favorite.py` 用的也是同一批假 URL，同样过不去。
⇒ **不是产品 bug，是夹具在「先抓后扣」那次改动之后失效了**。

## 为什么用 transport 注入，而不是放宽 SSRF

mock transport **不发真实请求**，压根不经过 DNS 校验 ⇒ 不削弱任何安全防护。
生产路径（`transport=None`）行为**逐字节不变**。

而且 mock 模式下**只装载能带 transport 的两个 fetcher**（wechat / generic），
其余专属 fetcher（douyin / pdf / github）根本不加载 ⇒
**e2e 栈在 mock 模式下不可能发出任何真实请求**。这一点是可验证的：
见 `content-service/tests/fetchers/test_e2e_mock.py`。

## 硬门控

只有 `STASHBOX_E2E_FETCH_MOCK=1` **且** `ENVIRONMENT` 不是 `prod` 时才启用。
生产环境误设这个变量会**直接抛错退出**（而不是静默启用假页面）——
生产上返回假正文比返回错误严重得多。
"""

from __future__ import annotations

import os
from urllib.parse import unquote

import httpx

#: 开关名。沿用仓里「显式开关 + 硬门控」的风格。
ENV_KEY = "STASHBOX_E2E_FETCH_MOCK"

#: 夹具正文的最低长度要求（`fetchers/parser.py::MIN_ARTICLE_CHARS` = 200）。
#: 低于这个长度 wechat fetcher 会判「多半是反爬空壳页」而报错。
_BODY_CHARS = 320

#: 夹具页模板。
#:
#: 结构对着**真公众号页**抄，因为 wechat fetcher 的解析器认这些标记：
#:   · `<title>标题 - 公众号名</title>`  → 标题要剥掉「 - 公众号名」后缀
#:   · `<a id="js_name">`                → 公众号名
#:   · `<em id="publish_time">`          → 发布时间
#:   · `<div id="js_content">…</div>`    → 正文（**唯一**的正文容器）
#:
#: `#js_content` 后面紧跟 `<script`：这是 fetcher 的**首选**正则
#: （非贪婪匹配到紧跟的 `</div><script`），比兜底正则（吃到文档末尾）准。
_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title} - 听匣 e2e 夹具</title>
<meta name="author" content="听匣 e2e 夹具">
<meta property="article:published_time" content="2026-01-01 12:34">
</head>
<body>
<a id="js_name">听匣 e2e 夹具</a>
<em id="publish_time">2026-01-01 12:34</em>
<div id="js_content">
<p>这是听匣端到端测试用的夹具正文，对应链接标识 {marker}。</p>
<p>夹具的作用是让「剪藏 → 建文章 → 派蒸馏任务」这条链路在**没有外网**的情况下
也能被真跑一遍：先抓后扣的策略要求正文必须真的抓到，否则入口会当场报错；
而 e2e 栈跑在本机，抓取器又有 SSRF 防护挡着回环地址，于是只能由这份夹具页
顶上真实站点的角色。</p>
<p>正文长度需要超过 {min_chars} 个字符，否则抓取器会判定这是反爬的空壳页而
直接失败 —— 那正是它对真实公众号文章的保护逻辑，在这里同样生效。</p>
<p>文章标识：{marker}。断言应该落在「库里能查到这篇文章」和「蒸馏任务被派出去」
这两件事上，而不是夹具正文的具体措辞。</p>
</div>
<script>window.__E2E_FIXTURE__ = "{marker}";</script>
</body>
</html>
"""


def enabled() -> bool:
    """这个开关现在是不是开着。"""
    return (os.getenv(ENV_KEY) or "").strip().lower() in ("1", "true", "yes", "on")


def _assert_not_production() -> None:
    """生产环境误设开关 ⇒ 直接炸，不静默启用假页面。

    理由：生产上返回一份假正文，用户会拿到「能剪藏但内容不对」的文章，
    而这类故障比「剪藏失败」难查得多。宁可进程起不来。
    """
    environment = (os.getenv("ENVIRONMENT") or "dev").strip().lower()
    if environment == "prod":
        raise RuntimeError(
            f"{ENV_KEY} 只允许在非生产环境开启（当前 ENVIRONMENT={environment}）—— "
            "生产上返回夹具正文比返回错误严重得多"
        )


def _marker_for(url: str) -> str:
    """从 URL 里取个可读的标识，夹具正文/标题里都带上它。

    取 URL 路径最后一段（`.../s/e2e_abc123` ⇒ `e2e_abc123`）；取不到就用 host。
    """
    path = url.split("?", 1)[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return unquote(tail) or url


def _handler(request: httpx.Request) -> httpx.Response:
    """给**任何**请求返回同一篇夹具页。"""
    marker = _marker_for(str(request.url))
    html = _TEMPLATE.format(
        title=f"e2e 夹具文章 {marker}",
        marker=marker,
        min_chars=_BODY_CHARS,
    )
    return httpx.Response(
        200,
        content=html.encode("utf-8"),
        headers={
            "content-type": "text/html; charset=utf-8",
            "content-length": str(len(html.encode("utf-8"))),
        },
    )


def build_transport() -> httpx.AsyncBaseTransport | None:
    """返回 mock transport；开关没开就返回 None（生产路径原样）。

    调用方（`fetchers/__init__.py`）拿到非 None 时**只装载带 transport 的
    fetcher**，从而保证 mock 模式下没有真实请求能发出去。
    """
    if not enabled():
        return None
    _assert_not_production()
    return httpx.MockTransport(_handler)
