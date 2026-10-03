"""内容合理性闸门（parser.MIN_ARTICLE_CHARS）的测试。

## 这条闸门是被真实端到端验证逼出来的，不是设计时想出来的

改造前抓取层只有**单段**密度启发式（`MIN_TEXT_DENSITY`）。本机实测一个 JS 占位页：

    <body><div id="app">loading</div><script src="/app.js"></script></body>

`ContentExtractor` 把 `<div id="app">loading</div>` 当成合法正文：
纯文本 7 字 / 原始 25 字 = 密度 **0.28 > 0.25** 阈值。

于是端到端结果是：剪藏**成功**，正文只有 7 个字 `"loading"`，
抓取层一声不响，没有任何异常可报。

这和上一轮修掉的「`[empty article]` 占位符被当正文喂给 LLM」是**同一类故障**：
失败伪装成成功，垃圾正文进蒸馏链烧 token。密度解决不了它 ——
因为壳页里确实存在一段"高密度"文本。

所以加了整篇合理性闸门：低于 200 字判为"没抓到正文"，抛 PARSE，
而 PARSE 是可升级错误码，正好触发降级链去试无头浏览器通道。
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


_load("cs_gate")
_parser = importlib.import_module("cs_gate.parser")
_base = importlib.import_module("cs_gate.base")
_generic = importlib.import_module("cs_gate.generic_url")
_wechat = importlib.import_module("cs_gate.wechat")

ContentExtractor = _parser.ContentExtractor
MIN_ARTICLE_CHARS = _parser.MIN_ARTICLE_CHARS
MIN_TEXT_DENSITY = _parser.MIN_TEXT_DENSITY
FetcherError = _base.FetcherError
FetcherErrorCode = _base.FetcherErrorCode
GenericURLFetcher = _generic.GenericURLFetcher
WechatFetcher = _wechat.WechatFetcher

#: 复现实测现场：JS 占位页。密度 0.28 稳稳高过 0.25 阈值。
JS_SHELL_HTML = (
    "<!doctype html><html><head><meta charset=utf-8>"
    "<title>JS渲染的标题</title></head>"
    '<body><div id="app">loading</div><script src="/app.js"></script></body></html>'
)


def test_documented_incident_shell_passes_single_block_density_check():
    """先钉住"事故为什么会发生"：单段密度启发式**确实拦不住**这个壳页。

    没有这条，后人很容易以为 MIN_TEXT_DENSITY 已经覆盖了这种情况，
    于是把整篇闸门当成冗余删掉 —— 那样剪藏会重新变成"成功但正文只有 7 个字"。
    """
    blocks = ContentExtractor().parse(JS_SHELL_HTML).best()
    assert blocks, "壳页里确实存在一段文本"
    assert blocks[0]["text"] == "loading"
    assert blocks[0]["density"] >= MIN_TEXT_DENSITY, (
        f"密度 {blocks[0]['density']} 高过阈值 {MIN_TEXT_DENSITY} —— "
        "这正是单段启发式失效的原因，删掉整篇闸门前请先想清楚"
    )


def test_shell_extracts_only_seven_chars_far_below_whole_article_floor():
    """整篇闸门的必要性：抽出来的东西离合理正文差两个数量级。"""
    text = ContentExtractor().parse(JS_SHELL_HTML).content_text()
    assert len(text) == 7
    assert len(text) < MIN_ARTICLE_CHARS


def test_min_article_chars_is_set_at_a_useful_floor():
    """阈值本身也要被锁：200 字 = 听完不到 1 分钟，对"听一篇文章"没有价值。"""
    assert MIN_ARTICLE_CHARS == 200


def test_generic_url_rejects_js_shell_as_parse_error():
    """JS 占位页必须判为"没抓到正文"（可升级错误码），不能假成功。"""
    acq = _pipeline_acquired(JS_SHELL_HTML)
    with pytest.raises(FetcherError) as ei:
        GenericURLFetcher().parse(acq)
    assert ei.value.code is FetcherErrorCode.PARSE
    assert "too short" in ei.value.message


def test_generic_url_accepts_realistic_article():
    """正常篇幅必须照常通过（闸门不能变成新的误杀源）。"""
    body = "<p>" + "这是一段正常的文章正文内容。" * 60 + "</p>"
    html = f"<html><head><title>正常文章</title></head><body>{body}</body></html>"
    acq = _pipeline_acquired(html)
    result = GenericURLFetcher().parse(acq)
    assert len(result.content_text) > MIN_ARTICLE_CHARS


def test_wechat_rejects_near_empty_js_content():
    """公众号也要这道闸：`#js_content` 存在 ≠ 有正文（反爬空壳页会带空容器）。"""
    acq = _pipeline_acquired('<div id="js_content"><p>短</p></div>', tier="browser")
    with pytest.raises(FetcherError) as ei:
        WechatFetcher().parse(acq)
    assert ei.value.code is FetcherErrorCode.PARSE
    assert "too short" in ei.value.message


def test_wechat_accepts_real_article_length_body():
    body = "<p>" + "公众号正文段落。" * 60 + "</p>"
    acq = _pipeline_acquired(f'<div id="js_content">{body}</div>', tier="browser")
    result = WechatFetcher().parse(acq)
    assert len(result.content_text) > MIN_ARTICLE_CHARS


def _pipeline_acquired(html: str, *, tier: str = "http"):
    """构造 pipeline 交给 parse 的 Acquired（用真 dataclass，不 mock）。"""
    _pipeline = importlib.import_module("cs_gate.pipeline")
    return _pipeline.Acquired(
        html=html,
        final_url="https://example.com/a",
        status=200,
        tier=tier,
        url="https://example.com/a",
    )
