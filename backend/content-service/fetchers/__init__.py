"""content-service 抓取器包（v1 §11.2 CP2.1）。

公共导出 + 工厂 get_fetcher()。CP2.1 只建抽象层，还没接入主流程
（接入在 CP2.5 服务号 Handler / CP2.7 集成测）。
"""

from __future__ import annotations

from stashbox.backend.common.exceptions import BizException

from .base import Fetcher, FetcherError, FetcherErrorCode, FetchResult
from .douyin import DouyinFetcher
from .generic_url import GenericURLFetcher
from .pdf import PdfFetcher
from .wechat import WechatFetcher

# 顺序 = 优先级：专属域名在前，通用兜底在后
_ALL_FETCHERS: list[Fetcher] = [
    WechatFetcher(),
    DouyinFetcher(),
    # CP11.0.8 P3.3: PDF 直链（URL path 以 .pdf 结尾才接）
    PdfFetcher(),
    GenericURLFetcher(),  # 通用放最后（catch-all，优先级最低）
]


def get_fetcher(url: str) -> Fetcher | None:
    """按 URL 匹配一个 fetcher（CP2.5 服务号 Handler 会用）。

    Returns:
        第一个 supports(url) 为 True 的 fetcher；理论上不会返回 None
        （GenericURLFetcher 是 catch-all）。
    """
    for fetcher in _ALL_FETCHERS:
        if fetcher.supports(url):
            return fetcher
    return None


# 抓取失败时给**终端用户**看的话术。
#
# 为什么必须是中文且可操作：剪藏是听匣的第一个动作，用户粘一个链接就想听。
# 抓取失败时如果只回一句 "fetch failed [wechat_mp/fetcher.auth]: ..."，
# 用户既看不懂也不知道下一步干什么 —— 他会以为是自己链接错了，反复重试，
# 或者直接以为 App 坏了。这是剪藏成功率真正的杀手，不是技术细节。
#
# 措辞原则：说清楚「谁的错」+「能不能重试」+「该做什么」。
# 技术细节（fetcher 名、异常类型）走日志，不进这个字符串。
_FETCH_ERROR_USER_MESSAGE: dict[FetcherErrorCode, str] = {
    FetcherErrorCode.UNSUPPORTED: "暂不支持这个链接来源，换个网站再试试",
    # 刻意不点破"内网/安全策略"：告诉攻击者"这个地址被识别了、我们拦了内网"，
    # 等于给 SSRF 探测一个免费 oracle。用户只需要知道"这个链接不能用"。
    #
    # ⚠️ 但**不能**和 UNSUPPORTED 用同一句话（2026-10-22 实测踩出来的）：
    #   实测 `https://news.ycombinator.com/item?id=1` 报的是这一类，而真实原因
    #   不是"不支持这个来源"——是**大陆 DNS 对境外域名投毒**，返回了
    #   `2001::caa0:81a4`（IANA 保留段）→ SSRF 防护按 IP fail-closed 拦截。
    #   对这种用户说"暂不支持这个链接来源，换个网站再试试"是**双重误导**：
    #   他剪的就是那个网站，换什么都没用，还会以为产品不支持这类站点。
    #   「我们暂时读不到内容」对两种真实情况都成立（真内网读不到、被投毒也读不到），
    #   又不泄露任何 SSRF 判定细节。
    FetcherErrorCode.SSRF_BLOCKED: "这个链接我们暂时读不到内容，换个来源试试",
    FetcherErrorCode.NETWORK: "没能连上这个网站，可能是网络不通或对方站点暂时不可用，请稍后重试",
    FetcherErrorCode.RATE_LIMIT: "对方网站暂时限制了访问，请过几分钟再试",
    # 通用话术。**微信文章专属的那句在 fetch_error_user_message() 里按 source 分流** ——
    # 同一个 AUTH，PDF / 普通网页说「微信文章请在微信里打开」是胡话
    # （实测 adobe.com 的 PDF 返回 403，用户看到的就是这句）。
    FetcherErrorCode.AUTH: "对方网站限制了非官方客户端访问，换个来源试试",
    FetcherErrorCode.NOT_FOUND: "这篇文章已被删除或链接已失效",
    FetcherErrorCode.PARSE: "没能提取出文章正文，页面可能需要登录后才能查看",
    # 单独成句，不复用 PARSE 的"可能需要登录"：对方要的是人机验证，不是登录。
    # 复用会让用户以为自己漏了登录动作，白花时间找不存在的原因。
    # 也不承诺"我们能过" —— 事实上多数情况下不能（实测百度 5 种浏览器姿态全被拦）。
    FetcherErrorCode.BOT_CHALLENGE: "这个网站要求人机验证后才能查看，听匣无法自动通过。可以换个来源的链接试试",
    FetcherErrorCode.INTERNAL: "抓取出错了，请稍后再试",
}


def fetch_error_user_message(code: FetcherErrorCode, source: str = "") -> str:
    """FetcherErrorCode → 面向用户的中文提示。

    `source` 用来**按抓取器区分措辞**：同一个错误码，不同站点的正确建议
    完全不同。实测踩到的：PDF 返回 403（fetcher.auth）时，用户看到的
    是「微信文章请在微信里打开后重新复制链接」—— 对一个 PDF 说这句话
    完全是胡话，而它当时还印在营销页宣传的能力上。
    """
    if code is FetcherErrorCode.AUTH and source.startswith("wechat"):
        return "对方网站限制了非官方客户端访问，微信文章请在微信里打开后重新复制链接"
    return _FETCH_ERROR_USER_MESSAGE.get(code, "抓取失败，请稍后重试")


def map_fetcher_error(exc: FetcherError) -> BizException:
    """FetcherError → BizException（v1 §3.x 文章模块错误码）。

    fetcher 私有错误码（fetcher.*）→ 业务错误码的**唯一映射点**，不散在 Handler 里：
    - UNSUPPORTED / SSRF_BLOCKED → 2001（这个链接不能用，HTTP 400）
    - NETWORK/PARSE/NOT_FOUND/AUTH/RATE_LIMIT/BOT_CHALLENGE → 2002（抓取失败，HTTP 502）
    - INTERNAL → 2002（抓取失败，HTTP 500）

    SSRF_BLOCKED 归到 2001 而不是 2002：被拦下是**用户提交的 URL 本身有问题**
    （指向内网），不是我们服务挂了。报 502 会让客户端以为是"我们这边故障"从而重试，
    而重试一个永远会被拦的链接毫无意义。

    码值和 HTTP 状态是对外契约，不要改；message 改成用户可读的中文，
    技术细节由调用方打日志（见 capture 端点的 fetch_fail 日志）。
    """
    if exc.code in (FetcherErrorCode.UNSUPPORTED, FetcherErrorCode.SSRF_BLOCKED):
        biz = BizException(code=2001, message=fetch_error_user_message(exc.code, exc.source))
        biz.detail = f"[{exc.source}/{exc.code.value}] {exc.message}"
        return biz

    biz = BizException(code=2002, message=fetch_error_user_message(exc.code, exc.source))
    biz.http_status = 500 if exc.code == FetcherErrorCode.INTERNAL else 502
    # 技术细节留在异常属性里，日志用；不进 message，避免把内部实现泄给客户端。
    biz.detail = f"[{exc.source}/{exc.code.value}] {exc.message}"
    return biz


__all__ = [
    "Fetcher",
    "FetchResult",
    "FetcherError",
    "FetcherErrorCode",
    "WechatFetcher",
    "DouyinFetcher",
    "GenericURLFetcher",
    "get_fetcher",
    "map_fetcher_error",
    "fetch_error_user_message",
]
