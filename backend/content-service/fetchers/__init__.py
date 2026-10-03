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
    FetcherErrorCode.NETWORK: "没能连上这个网站，可能是网络不通或对方站点暂时不可用，请稍后重试",
    FetcherErrorCode.RATE_LIMIT: "对方网站暂时限制了访问，请过几分钟再试",
    FetcherErrorCode.AUTH: "对方网站限制了非官方客户端访问，微信文章请在微信里打开后重新复制链接",
    FetcherErrorCode.NOT_FOUND: "这篇文章已被删除或链接已失效",
    FetcherErrorCode.PARSE: "没能提取出文章正文，页面可能需要登录后才能查看",
    FetcherErrorCode.INTERNAL: "抓取出错了，请稍后再试",
}


def fetch_error_user_message(code: FetcherErrorCode) -> str:
    """FetcherErrorCode → 面向用户的中文提示。"""
    return _FETCH_ERROR_USER_MESSAGE.get(code, "抓取失败，请稍后重试")


def map_fetcher_error(exc: FetcherError) -> BizException:
    """FetcherError → BizException（v1 §3.x 文章模块错误码）。

    fetcher 私有错误码（fetcher.*）→ 业务错误码的**唯一映射点**，不散在 Handler 里：
    - UNSUPPORTED → 2001（URL 不支持，HTTP 400）
    - NETWORK/PARSE/NOT_FOUND/AUTH/RATE_LIMIT → 2002（抓取失败，HTTP 502）
    - INTERNAL → 2002（抓取失败，HTTP 500）

    码值和 HTTP 状态是对外契约，不要改；message 改成用户可读的中文，
    技术细节由调用方打日志（见 capture 端点的 fetch_fail 日志）。
    """
    if exc.code == FetcherErrorCode.UNSUPPORTED:
        biz = BizException(code=2001, message=fetch_error_user_message(exc.code))
        biz.detail = f"[{exc.source}/{exc.code.value}] {exc.message}"
        return biz

    biz = BizException(code=2002, message=fetch_error_user_message(exc.code))
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
