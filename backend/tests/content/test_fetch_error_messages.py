"""面向用户的抓取失败话术（2026-10-22）。

放在 backend/tests/content/ 下（不是 content-service/tests/fetchers/）——
pytest 的 testpaths 只有 ["tests"]，CI 也只跑 `pytest tests/`，
放在 content-service 下面等于这个测试永远不会被执行。

## 为什么要有这个测试

`SSRF_BLOCKED` 与 `UNSUPPORTED` 曾经共用一句话「暂不支持这个链接来源，
换个网站再试试」。实测证明那句话**在 SSRF 那一类上是误导**：

    实测 https://news.ycombinator.com/item?id=1 →
    code=fetcher.ssrf_blocked
    msg=host news.ycombinator.com resolves to blocked address: 2001::caa0:81a4

真实原因是**大陆 DNS 对境外域名投毒**（返回 IANA 保留段的 IPv6），
SSRF 防护按 IP fail-closed 拦截 —— 不是「产品不支持这类站点」。

对这种用户说「换个网站再试试」是双重误导：他剪的就是那个网站，换什么
都没用；还会以为产品不支持，于是直接放弃。

所以这里锁三件事：
1. 两句话**不能再合并**（合并即回归）；
2. SSRF 那句**不能**出现「不支持来源 / 换个网站」这种误导措辞；
3. SSRF 那句**不能**泄露拦截细节（内网 / 安全策略 / IP）—— 那是给
   SSRF 探测者的免费 oracle，注释里明确写过这条红线。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2] / "content-service"
if str(CONTENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_SERVICE_DIR))


def _load_fetchers():
    pkg_dir = CONTENT_SERVICE_DIR / "fetchers"
    spec = importlib.util.spec_from_file_location(
        "cs_fetchers_msgs",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cs_fetchers_msgs"] = mod
    spec.loader.exec_module(mod)
    return mod


cs_fetchers = _load_fetchers()
FetcherErrorCode = cs_fetchers.FetcherErrorCode
fetch_error_user_message = cs_fetchers.fetch_error_user_message


def test_ssrf_与_unsupported_的话术不能是同一句():
    """合并即回归：SSRF 那类的真实原因不是「不支持这个来源」。"""
    a = fetch_error_user_message(FetcherErrorCode.SSRF_BLOCKED)
    b = fetch_error_user_message(FetcherErrorCode.UNSUPPORTED)
    assert a != b, "SSRF_BLOCKED 与 UNSUPPORTED 必须是不同的话术"


def test_ssrf_话术不含误导措辞():
    msg = fetch_error_user_message(FetcherErrorCode.SSRF_BLOCKED)
    for bad in ("不支持", "换个网站"):
        assert bad not in msg, f"SSRF 话术里不该出现「{bad}」：{msg}"


def test_ssrf_话术不泄露拦截细节():
    """内网 / 安全策略 / IP 这些词不能说出去 —— 等于给 SSRF 探测 oracle。"""
    msg = fetch_error_user_message(FetcherErrorCode.SSRF_BLOCKED)
    for leak in ("内网", "安全策略", "拦截", "保留段", "IP", "ip 地址"):
        assert leak not in msg, f"SSRF 话术泄露了细节「{leak}」：{msg}"


def test_unsupported_话术仍然点明是来源不支持():
    """真的不支持（scheme 不对之类）时，用户该知道是来源问题。"""
    msg = fetch_error_user_message(FetcherErrorCode.UNSUPPORTED)
    assert "不支持" in msg


def test_每个错误码都有话术():
    for code in FetcherErrorCode:
        msg = fetch_error_user_message(code)
        assert msg, f"{code} 没有话术"
        assert "抓取失败" not in msg or code is FetcherErrorCode.INTERNAL, f"{code} 用了兜底话术"


def test_未知错误码有兜底话术():
    assert fetch_error_user_message("不存在的码") == "抓取失败，请稍后重试"


def test_auth_话术按抓取器分流():
    """同一个 AUTH，微信文章与别的站点要说得不一样。

    实测踩到的：PDF 返回 403（fetcher.auth）时，用户看到的是
    「微信文章请在微信里打开后重新复制链接」—— 对一个 PDF 说这句话
    完全是胡话，而 PDF 是营销页宣传过的能力。
    """
    wechat_msg = fetch_error_user_message(FetcherErrorCode.AUTH, "wechat_mp")
    other_msg = fetch_error_user_message(FetcherErrorCode.AUTH, "pdf")
    assert "微信" in wechat_msg, "微信文章该告诉用户回微信里重新复制链接"
    assert "微信" not in other_msg, f"非微信来源不该提微信：{other_msg}"
    assert wechat_msg != other_msg


def test_auth_通用话术不含误导指引():
    msg = fetch_error_user_message(FetcherErrorCode.AUTH, "generic_url")
    for bad in ("微信", "换个网站"):
        assert bad not in msg, f"通用 AUTH 话术不该出现「{bad}」：{msg}"


# ── PARSE（链接剪藏）：不能只说「需要登录」（2026-10-25） ────────────────
#
# 背景（实测，不是推测）：生产上 `STASHBOX_FETCH_BROWSER_TIER=0`
# —— chromium RSS 300MB+ 而 content-service 的 MemoryMax 只有 224MB，
# 开浏览器通道等于埋 OOM ⇒ **生产剪藏只走 http 通道**。
#
# 而 http 通道提不出正文的真实成因有**两类**：
#   ① 页面要登录；
#   ② 内容由网页脚本动态加载（SPA / 前端渲染站）。
# 实测 `https://example.org/` 就是第 ②类：http 通道报
# `no article content extracted`，靠升级 browser 才成功
# —— 而 browser 通道在生产是关的 ⇒ 这类页面**生产必然剪不动**。
#
# 旧文案「没能提取出文章正文，页面可能需要登录后才能查看」把 ①说成了唯一可能：
# 对 ② 的用户是**误导** —— 他会以为自己漏了登录动作，去登录、再重试，
# 而结果永远不会变（这正是上一轮内测反馈「剪藏不成功」的一类来源）。
#
# 所以这里锁四件事：
#   1. 不能只提「登录」；
#   2. 要提到「网页加载/脚本」这一类真实成因；
#   3. 要让用户知道这类页面**暂时读不了**（别干等、别反复重试）；
#   4. 长度有上限 —— 安卓端是 `Toast.LENGTH_SHORT`（CaptureScreen.kt），
#      太长会被截断，反而什么都读不到。
#
# 文档剪藏的 PARSE 是**独立分支**（加密 PDF / OCR 认不出字），
# 不能混进链接这句 —— 文档根本没有「登录」这回事。


def test_parse_链接剪藏话术不只说登录():
    """只说「需要登录」对 JS 渲染页面是误导 ⇒ 回归即失败。"""
    msg = fetch_error_user_message(FetcherErrorCode.PARSE, "generic_url")
    assert "网页" in msg or "加载" in msg, f"PARSE 话术该提到网页加载这一类成因：{msg}"


def test_parse_链接剪藏话术点明暂时读不了():
    """要让用户知道等与重试都没用 ⇒ 引导他换个链接。"""
    msg = fetch_error_user_message(FetcherErrorCode.PARSE, "generic_url")
    assert "暂时" in msg or "读不了" in msg, f"PARSE 话术该点明这类页面暂时读不了：{msg}"


def test_parse_链接剪藏话术长度在_toast_装得下的范围():
    """安卓端是 Toast.LENGTH_SHORT ⇒ 超过 ~45 字会被截断。"""
    msg = fetch_error_user_message(FetcherErrorCode.PARSE, "generic_url")
    assert len(msg) <= 45, f"PARSE 话术过长（{len(msg)} 字），Toast 装不下：{msg}"


def test_parse_文档剪藏仍是独立分支():
    """文档没有「登录」这回事 ⇒ 不能沿用链接那句。"""
    doc = fetch_error_user_message(FetcherErrorCode.PARSE, "document")
    link = fetch_error_user_message(FetcherErrorCode.PARSE, "generic_url")
    assert doc != link, "文档剪藏的 PARSE 话术必须与链接剪藏分开"
    assert "网页" not in doc, f"文档话术不该提网页加载：{doc}"
