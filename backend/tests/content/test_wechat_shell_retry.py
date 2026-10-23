"""微信反爬空壳 → 带 `nwr_flag=1` 重试（2026-10-23）。

## 背景

微信对同一 IP 的**突发**请求会返回一个**小空壳页**（实测 39B / 31KB）而不是
正文。壳页里能看到「参数错误」提示、`<title>` 是空的、没有 `#js_content` ——
解析器于是报「wechat article body (#js_content) not found (page too small: 39B …）」，
用户看到的是 502「暂时连不上后端服务」。

而微信**自己给出了绕过办法**：成功的请求会被重定向到
`…?nwr_flag=1#wechat_redirect`，且**显式带 `?nwr_flag=1` 请求则稳定拿到正文**
（实测 3/3，每次 3.5MB、js_content 18 处）。

之前代码拿到壳页**直接判失败**，一次偶发反爬就变成用户可见的失败。

## 这里锁住的行为

1. 壳页（无 `#js_content` + 体积小或命中提示词）→ **重试一次并带 nwr_flag=1**；
2. 重试拿到真文章 → 用它；
3. 重试还是壳页 → 交回原来的结果（错误码由上层按原路径判定，不变味）；
4. **不能**把真文章误判成壳页（真文章页的 webpack 载荷里天然带
   「请在微信中打开」等文案 11 处 —— 单看提示词会 100% 误伤）。
"""

from __future__ import annotations

import pytest

from fetchers import wechat as wx  # noqa: E402  - 与 main.py 内部同一套模块


async def _no_sleep(*_args, **_kwargs):
    """测试里不等退避（真实退避 1.5s，几十个用例就是纯等待）。"""
    return None


def test_壳页_被识别出来():
    """39B 的空壳（实测真实体积）→ 是壳页。"""
    assert wx._is_wechat_shell("<html><body>参数错误</body></html>")


def test_17kb_的空壳页_被识别出来():
    """实测那个壳页 **17,626 字节**（超过 10KB 阈值），但**可见文本只有 43 字节**。

    它里面**一个提示词都没有**（不是「参数错误」也不是「环境异常」）——
    所以判据只能是「没有 #js_content + 可见文本极少」。
    """
    css = "body{margin:0}" * 1200  # 微信自己的 CSS，占掉绝大多数体积
    html = (
        f"<html><head><title></title><style>{css}</style></head>"
        "<body><span>： ， 。 &nbsp; 视频 小程序 赞 ，轻点两下取消赞 在看</span></body></html>"
    )
    assert len(html.encode("utf-8")) > wx.MIN_SHELL_BYTES, "这个壳页必须大于 10KB"
    assert "参数错误" not in html, "实测壳页里没有提示词"
    assert wx._is_wechat_shell(html)


def test_真文章_绝不能被判成壳页():
    """真文章页的 webpack 载荷里天然带「请在微信中打开」11 处。

    单看提示词会把**每一篇正常文章**都判成壳 —— 那就是 100% 误伤。
    """
    real = (
        "<html><body>"
        '<div id="js_content">' + "正文内容" * 100 + "</div>"
        "<script>var x='请在微信中打开 环境异常';</script>"
        "</body></html>"
    )
    assert not wx._is_wechat_shell(real)


def test_有_js_content_但可见文本短_也不算壳页():
    """正文容器在 → 放行（哪怕正文短）。判据的第一优先级是「有没有正文容器」。"""
    html = '<html><body><div id="js_content">短</div></body></html>'
    assert not wx._is_wechat_shell(html)


def test_加_nwr_flag_不覆盖已有参数():
    out = wx._with_nwr_flag("https://mp.weixin.qq.com/s/abc")
    assert "nwr_flag=1" in out
    assert out.startswith("https://mp.weixin.qq.com/s/abc")


def test_加_nwr_flag_保留已有_query():
    out = wx._with_nwr_flag("https://mp.weixin.qq.com/s/abc?chksm=123")
    assert "chksm=123" in out
    assert "nwr_flag=1" in out


def test_加_nwr_flag_不重复加():
    once = wx._with_nwr_flag("https://mp.weixin.qq.com/s/abc")
    twice = wx._with_nwr_flag(once)
    assert twice.count("nwr_flag") == 1


@pytest.mark.asyncio
async def test_壳页_会触发_nwr_flag_重试(monkeypatch):
    """第一次给壳页、第二次给真文章 → 最终用真文章。"""
    calls: list[str] = []

    async def fake_download_once(self, url, *, timeout):
        calls.append(url)
        if len(calls) == 1:
            return "<html><body>参数错误</body></html>", 200, url
        return '<div id="js_content">正文</div>', 200, url

    monkeypatch.setattr(wx.WechatFetcher, "_download_once", fake_download_once)
    monkeypatch.setattr(wx.asyncio, "sleep", _no_sleep)

    f = wx.WechatFetcher()
    html, status, final = await f._download("https://mp.weixin.qq.com/s/abc", timeout=10)

    assert len(calls) == 2, "应该重试一次"
    assert "nwr_flag=1" in calls[1], f"重试要带 nwr_flag=1：{calls[1]}"
    assert "js_content" in html, "应该用重试拿到的真文章"


@pytest.mark.asyncio
async def test_重试_还是壳页_就交回原来的结果(monkeypatch):
    """重试也失败 → 错误码由上层按原路径判定，**不要**在这里改味。"""
    calls: list[str] = []

    async def fake_download_once(self, url, *, timeout):
        calls.append(url)
        return "<html><body>参数错误</body></html>", 200, url

    monkeypatch.setattr(wx.WechatFetcher, "_download_once", fake_download_once)
    monkeypatch.setattr(wx.asyncio, "sleep", _no_sleep)

    f = wx.WechatFetcher()
    html, status, final = await f._download("https://mp.weixin.qq.com/s/abc", timeout=10)

    assert len(calls) == 2
    assert "参数错误" in html


@pytest.mark.asyncio
async def test_一次成功_不重试(monkeypatch):
    """真文章一次拿到 → **不要**多打一次（省一次上游请求，也省一次被限流的机会）。"""
    calls: list[str] = []

    async def fake_download_once(self, url, *, timeout):
        calls.append(url)
        return '<div id="js_content">正文</div>', 200, url

    monkeypatch.setattr(wx.WechatFetcher, "_download_once", fake_download_once)

    f = wx.WechatFetcher()
    await f._download("https://mp.weixin.qq.com/s/abc", timeout=10)

    assert len(calls) == 1, "一次就成功时不该重试"
