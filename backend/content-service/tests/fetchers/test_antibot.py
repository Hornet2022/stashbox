"""人机验证识别（antibot.py）+ 错误择优上报的测试。

## 为什么这组判据值得锁死

本机实测百度百家号对同一篇文章给出**两种不同的拦截**：

    A. 302 -> wappass.baidu.com/static/captcha/tuxing.html   <title>百度安全验证</title>
    B. 200  mbd.baidu.com/newspage/data/error                正文「这里空空如也」6 字

A 能被识别成 BOT_CHALLENGE，B 只能报 PARSE。这意味着**单次测量给不出稳定结论**，
所以下面的用例必须覆盖**两条路径**，而不是只测一个"看起来会过"的样本。

另外两条容易被后人改坏的规则也在这里锁住：
- 全页子串匹配会误判正文页（wechat `_check_wechat_block` 已经踩过这个坑）
- PARSE 是兜底类别，不许盖掉一个已经查明原因的错误
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


_load("cs_antibot")
_base = importlib.import_module("cs_antibot.base")
_antibot = importlib.import_module("cs_antibot.antibot")
_pipeline = importlib.import_module("cs_antibot.pipeline")

FetcherError = _base.FetcherError
FetcherErrorCode = _base.FetcherErrorCode
detect_bot_challenge = _antibot.detect_bot_challenge
_pick_better_error = _pipeline._pick_better_error

SOURCE = "test"

# 实测样本 A：百度硬验证码（302 后的落地页）
CAPTCHA_HTML = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    "<title>百度安全验证</title></head><body>请完成下方验证后继续访问</body></html>"
)
CAPTCHA_URL = "https://wappass.baidu.com/static/captcha/tuxing.html?ak=572be823e2f50ea759a616"

# 实测样本 B：百度软壳（HTTP 200，正文只有「这里空空如也」）
SOFT_BLOCK_HTML = "<html><body><div>这里空空如也</div></body></html>"
SOFT_BLOCK_URL = "https://mbd.baidu.com/newspage/data/error?id=1852183554426518629"


# -- 识别：硬验证码 ---------------------------------------------------------


def test_detects_captcha_by_landing_host():
    """落地域名是验证域名 —— 最强信号，正常文章页不会跨域跳到 wappass。"""
    with pytest.raises(FetcherError) as ei:
        detect_bot_challenge(CAPTCHA_HTML, final_url=CAPTCHA_URL, source=SOURCE)
    assert ei.value.code is FetcherErrorCode.BOT_CHALLENGE
    assert "wappass.baidu.com" in ei.value.message


def test_detects_captcha_by_title():
    """没有 host 命中时，<title> 是次强信号。"""
    with pytest.raises(FetcherError) as ei:
        detect_bot_challenge(CAPTCHA_HTML, final_url="https://example.com/a", source=SOURCE)
    assert ei.value.code is FetcherErrorCode.BOT_CHALLENGE


# -- 不误判：正文页 ---------------------------------------------------------


def test_normal_article_passes():
    url = "https://baijiahao.baidu.com/s?id=123"
    html = f'<html><head><title>一篇正常文章 - 百家号</title></head><body><div id="content"><p>{"正常正文内容。" * 200}</p></div></body></html>'
    detect_bot_challenge(html, final_url=url, source=SOURCE)  # 不抛 = 通过


def test_challenge_words_in_script_do_not_trigger_false_positive():
    """**回归守卫**：整页子串匹配会误判正文页。

    微信那边已经吃过一次亏 —— 真文章页的 webpack 载荷里天然带"请在微信中打开"
    11 处，朴素匹配导致线上 100% 误判。同样的坑不能再踩第二次：
    挑战词只出现在 <script> 里时不算命中。
    """
    html = (
        "<html><head><title>正常文章</title>"
        "<script>var tips=['安全验证','请输入验证码','百度安全验证'];</script>"
        "</head><body><div id='content'><p>真实正文</p></div></body></html>"
    )
    detect_bot_challenge(html, final_url="https://example.com/a", source=SOURCE)


def test_challenge_words_in_invisible_style_do_not_trigger():
    html = (
        "<html><head><title>正常文章</title></head><body>"
        "<style>.x:after{content:'安全验证'}</style>"
        "<p>真实正文</p></body></html>"
    )
    detect_bot_challenge(html, final_url="https://example.com/a", source=SOURCE)


# -- 实测样本 B：不硬编站点规则 --------------------------------------------


def test_soft_block_shell_is_not_classified_as_challenge():
    """实测样本 B（百度软壳）**刻意不**判成 BOT_CHALLENGE。

    `mbd.baidu.com/newspage/data/error` 是百度私有的"内容不可用"路径，
    把它硬编进通用规则等于过拟合到一次观测：百度改一次路径规则就失效，
    而对其他站点零收益。它报 PARSE（"没提取出正文"）在事实上也没说错。

    这条用例的作用是**把"不做过拟合"这个决定钉住**，
    免得后人看到实测失败就顺手加一条 baijiahao 专属规则。
    """
    detect_bot_challenge(SOFT_BLOCK_HTML, final_url=SOFT_BLOCK_URL, source=SOURCE)


# -- 错误择优 ---------------------------------------------------------------


def test_generic_parse_never_overwrites_a_specific_cause():
    """核心规则：PARSE 是兜底类别，不许盖掉已查明原因的错误。

    这条是实测踩出来的 —— 百家号在 HTTP 通道被判 BOT_CHALLENGE（说清了
    "对方要人机验证"），浏览器通道只能报 PARSE。按"取最后一个"上报的话，
    用户看到的是"没提取出正文"，真正原因被抹平。
    """
    specific = FetcherError(code=FetcherErrorCode.BOT_CHALLENGE, message="撞验证码", source=SOURCE)
    generic = FetcherError(code=FetcherErrorCode.PARSE, message="no article content", source=SOURCE)
    assert _pick_better_error(generic, specific) is specific
    assert _pick_better_error(specific, generic) is specific


def test_specificity_ordering():
    """具体度排序：谁更"知道原因"谁优先。"""
    codes = [
        FetcherErrorCode.PARSE,
        FetcherErrorCode.NETWORK,
        FetcherErrorCode.AUTH,
        FetcherErrorCode.BOT_CHALLENGE,
    ]
    best = None
    for code in codes:
        best = _pick_better_error(best, FetcherError(code=code, message="m", source=SOURCE))
    assert best.code is FetcherErrorCode.BOT_CHALLENGE


def test_equal_specificity_keeps_first_for_predictable_behaviour():
    """相同具体度时不替换，保证"先到先得"，行为可预测。"""
    first = FetcherError(code=FetcherErrorCode.PARSE, message="first", source=SOURCE)
    second = FetcherError(code=FetcherErrorCode.PARSE, message="second", source=SOURCE)
    assert _pick_better_error(first, second) is first
