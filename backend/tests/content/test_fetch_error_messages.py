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
